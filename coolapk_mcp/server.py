"""MCP Server — 酷安社区只读工具集（17 个工具）"""

import asyncio
import contextlib
import functools
import hashlib
import hmac
import inspect
import json
import logging
import os
import secrets
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings

from coolapk_mcp import __version__, throttle
from coolapk_mcp import postfilter
from coolapk_mcp import scoring


from coolapk_mcp.boards import BOARD_URLS
from coolapk_mcp.budget import Budget, BudgetExceeded
# FOCUS_MAX_PAGES 用冗余别名导入即为「显式再导出」：测试与 CLI 都靠 server 上的这个名字
from coolapk_mcp.focus import FOCUS_MAX_PAGES as FOCUS_MAX_PAGES
from coolapk_mcp.focus import collect_focus
from coolapk_mcp.client import (
    CoolapkClient,
    CoolapkError,
    aclose_image_client,
    image_client,
)
from coolapk_mcp.models import (
    AppModel,
    FeedDetail,
    FeedModel,
    ReplyModel,
    TopicModel,
    UserModel,
    _format_time,
    _strip_html as _strip_html_keep_links,
    apply_tool_whitelist,
    cap_replies,
    REPLY_MAX_ITEMS,
)
from coolapk_mcp.config import CONFIG_DIR, AppConfig, restrict_to_owner
# 图片中转的域名白名单与签名拼装只有一份实现（CLI image-link 也用这份）
from coolapk_mcp.img_link import IMAGE_HOSTS, build_image_links, split_urls
from coolapk_mcp.linkparse import parse_feed_id
from coolapk_mcp.logging_utils import setup_logging
IMAGE_UA = (
    "Dalvik/2.1.0 (Linux; U; Android 16; 23113RKC6C Build/AQ3A.250226.002) "
    "(#Build; Redmi; 23113RKC6C; AQ3A.250226.002; HyperOS_3.0; 3.0.1.0) "
    "+CoolMarket/16.2.0-2604201-universal"
)

# FOCUS_MAX_PAGES 由 focus 模块提供（server 与 CLI 共用一份实现）
# coolapk_notify 支持的通知类型（与 client.get_notifications 的 ntype 一致）
NOTIFY_TYPES = (
    "atMeMeFeed", "atComment", "comment", "feedLike", "contactsFollow", "message",
)
# /img 中转的单次响应体上限（15MB）：超限直接拒绝，避免单张大图把进程内存撑爆
MAX_IMAGE_BYTES = 15 * 1024 * 1024
# /img 签名密钥的落盘位置（未配置 COOLAPK_HTTP_TOKEN 时使用）
IMG_SECRET_FILE = CONFIG_DIR / "img_secret"


def _resolve_img_secret(token: str) -> str:
    """/img 的 HMAC 签名密钥。

    优先级：COOLAPK_IMG_SECRET → 本机持久化随机密钥 → 本次进程内临时随机密钥。

    历史实现回落到硬编码 "coolapk-mcp-dev"：只要服务对公网可达，任何人都能
    用公开字符串伪造 /img 链接，等于把它变成开放图片代理。
    也不复用 COOLAPK_HTTP_TOKEN：/img 链接会出现在聊天记录与日志里，
    拿签名串离线爆破 token 等于把 Bearer 凭据暴露在另一种信道上。
    """
    del token  # 保留参数以兼容调用方，但不再作为签名密钥来源
    env_secret = os.environ.get("COOLAPK_IMG_SECRET", "").strip()
    if env_secret:
        return env_secret
    try:
        existing = IMG_SECRET_FILE.read_text(encoding="utf-8").strip()
        if len(existing) >= 16:
            return existing
    except OSError:
        pass
    secret = secrets.token_urlsafe(32)
    try:
        IMG_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            # 先按 0600 建文件再写，避免 write_text 沿用 umask 出现短暂 0644 窗口
            fd = os.open(IMG_SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(secret)
        else:
            IMG_SECRET_FILE.write_text(secret, encoding="utf-8")
        restrict_to_owner(IMG_SECRET_FILE)
    except OSError as exc:
        logger.warning(
            "无法持久化 /img 签名密钥（%s）：本次运行使用临时密钥，"
            "重启后此前生成的所有 /img 链接都会失效；"
            "容器部署请确认挂载目录属主与容器 uid 一致（见 README）",
            IMG_SECRET_FILE,
            # 默认不打整段 Traceback（stderr 会刷屏）；排查时用 COOLAPK_LOG_LEVEL=DEBUG
            exc_info=exc if logger.isEnabledFor(logging.DEBUG) else None,
        )
    return secret


logger = logging.getLogger("coolapk_mcp.server")


@contextlib.asynccontextmanager
async def _server_lifespan(_server: Any):
    """MCP 服务生命周期：退出时释放全局 httpx 连接池。

    stdio 模式进程结束前会走这里（此前没有关闭钩子，连接池与后台任务悬空到
    进程被强杀）；HTTP 模式由 _build_http_app 的 Starlette lifespan 负责，
    两处都调 _reset_client()，它是幂等的（client 已为 None 时直接返回）。
    """
    try:
        yield None
    finally:
        await _reset_client()
        # 退休队列里的旧实例此时已无人持有，统一关闭
        await _close_retired()
        # 图片连接池同为进程级资源，stdio 退出时一并释放
        await aclose_image_client()


def _warn_token_weakness(token: str) -> None:
    """启动自检：弱 token 不阻止启动（会打断既有部署），但必须显式提醒。

    这些配置错误不会报错，只会表现成「客户端莫名 401」，没有日志提示极难定位。
    """
    if not token:
        return
    if token != token.strip():
        logger.warning("COOLAPK_HTTP_TOKEN 含首尾空白：部分客户端会带成不可见字符，导致 401")
    if len(token) < 16:
        logger.warning(
            "COOLAPK_HTTP_TOKEN 仅 %d 位，建议至少 32 位随机串"
            '（python -c "import secrets;print(secrets.token_urlsafe(32))"）',
            len(token),
        )
    if token == "coolapk-mcp-dev":
        logger.warning("COOLAPK_HTTP_TOKEN 仍是示例值 coolapk-mcp-dev，公网部署请立即更换")


def _transport_security_settings() -> TransportSecuritySettings:
    """DNS rebinding 防护按需开启。

    默认关闭是为了兼容「Host: 公网IP:端口」的直连部署（FastMCP 默认的 Host
    白名单会把这类合法请求判成 421）。配置 COOLAPK_ALLOWED_HOSTS 后开启防护，
    只放行列出的 Host（每项形如 127.0.0.1:8000 或 mcp.example.com）。
    """
    hosts = [
        item.strip()
        for item in os.environ.get("COOLAPK_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    ]
    origins = [
        item.strip()
        for item in os.environ.get("COOLAPK_ALLOWED_ORIGINS", "").split(",")
        if item.strip()
    ]
    if not hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins or ["*"],
    )


mcp = FastMCP(
    "coolapk",
    # 显式关闭 DNS rebinding 保护：FastMCP 在 host 为 127.0.0.1 时会自动开启，
    # 该校验按 Host 头白名单拦截，「Host: <公网IP>:端口」的合法直连（远程部署常态）
    # 会被误判 421；Bearer 鉴权已由 _build_http_app 强制，防护足够
    transport_security=_transport_security_settings(),
    # stdio 模式的关闭钩子：进程退出时关闭全局 client（HTTP 模式用 Starlette lifespan）
    lifespan=_server_lifespan,
    instructions=(
        "酷安社区只读工具集：搜索帖子/用户/话题/应用、查看帖子详情与评论、"
        "用户资料与动态、话题详情、首页动态、登录状态、图片下载、通用只读接口透传、"
        "作者画像与作者深挖聚合。"
        "全部工具只读，不会发帖、点赞或关注。"
        "搜索默认启用严格关键词过滤与去重（strict=False 可关闭）；"
        "帖子详情在详情接口被风控时会自动回落到作者动态列表（fetch_source 字段标注来源）；"
        "评论搜索（comment_keyword）最多翻 COOLAPK_MAX_REPLY_PAGES 页（默认 30），"
        "撞上限时 replies_truncated=True，可据 replies_next_page 继续。"
        "所有输出均经过白名单裁剪，只保留 agent 需要的核心字段；"
        "接口返回中的 cookie/token 等敏感字段一律剔除。"
        "帖子结果带 post_score（点赞+评论×3+转发×2）与 discussion_ratio，可直接用于排序筛选；"
        "判断教程可信度先看作者 author_tier（top/expert/active/normal），再决定是否用 "
        "coolapk_author_digest 深挖该作者的全部攻略。"
        "图片：若客户端显示不了 base64 图片（如手机语音助手、IM 机器人），"
        "用 coolapk_image_link 把图片 URL 转成签名 https 外链后再展示；"
        "本地 stdio 模式请直接用 coolapk_read_image。"
    ),
)

_client: CoolapkClient | None = None
# 当前 client 对应的配置指纹（uid/token/desktop_cookie/api_base），用于配置热加载
_client_fp: tuple[str, ...] | None = None
# 串行化 client 创建/重建，避免并发工具调用各建一个 httpx 连接池
_client_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    """惰性创建全局锁：不在 import 期构造，避免绑定到某个已关闭的事件循环。"""
    global _client_lock
    if _client_lock is None:
        _client_lock = asyncio.Lock()
    return _client_lock


def _config_fingerprint(config: AppConfig) -> tuple[str, ...]:
    """登录态与接口地址指纹：任一项变化即视为需要重建 client。

    device_code 也必须在内：X-App-Token 由它派生，换机/重装后设备码变了，
    但 uid/token 可能一模一样——漏掉它就会一直用旧设备码签 token。
    """
    return (
        str(config.uid or ""),
        str(config.token or ""),
        str(config.desktop_cookie or ""),
        str(config.api_base or ""),
        str(config.device_code or ""),
    )


# 退休队列：热加载/登录重置时把旧 client 放进来，延后到 lifespan 收尾统一关闭。
# 立刻关会让「已取到旧实例、正在发请求」的并发工具撞 httpx RuntimeError。
_retired: list[CoolapkClient] = []


def _retire(old: CoolapkClient | None) -> None:
    """把旧 client 交给退休队列（不立即关闭）。"""
    if old is not None:
        _retired.append(old)


async def _close_retired() -> None:
    """关闭所有退休 client；lifespan 收尾调用。"""
    while _retired:
        await _close_client(_retired.pop())


async def _close_client(old: CoolapkClient | None) -> None:
    """关闭 client，吞掉关闭异常（旧连接池的异常不该影响工具返回）。"""
    if old is None:
        return
    # CoolapkClient 暴露 close()，假对象/未来版本可能叫 aclose()，两者都兼容
    closer = getattr(old, "aclose", None) or getattr(old, "close", None)
    if closer is None:
        return
    try:
        result = closer()
        if inspect.isawaitable(result):
            await result
    except Exception:  # noqa: BLE001 —— 关闭失败只记日志，不向上抛
        logger.warning("关闭旧的 CoolapkClient 失败（已忽略）", exc_info=True)


async def _reset_client() -> None:
    """丢弃当前 client：先摘引用（后续调用会重建），再关闭旧实例。

    持锁只覆盖「摘引用」这一步，真正的 aclose 在锁外执行，
    避免关闭过程中的等待占住锁、让并发工具调用整体卡住。
    """
    global _client, _client_fp
    async with _get_lock():
        old = _client
        _client = None
        _client_fp = None
    _retire(old)


async def _get_client() -> CoolapkClient:
    """取当前 client（惰性创建）；配置指纹变化时关闭旧实例并重建。

    每次调用都读一次 AppConfig.load()：桌面端重新登录、config.json 被改写后，
    下一个工具调用即生效（配置热加载），无需重启 MCP 进程。
    锁只包住「比对指纹 + 换实例」，构造与关闭都不嵌套持锁，避免死锁。
    """
    global _client, _client_fp
    try:
        config: AppConfig | None = AppConfig.load()
        fp = _config_fingerprint(config) if config is not None else _client_fp
    except Exception:  # noqa: BLE001 —— 配置读坏了不阻断工具调用，沿用现状
        logger.warning("读取 AppConfig 失败，沿用现有 client", exc_info=True)
        config, fp = None, _client_fp

    async with _get_lock():
        if _client is not None and fp == _client_fp:
            return _client
        old = _client
        _client = None
        _client_fp = None
        replacement = CoolapkClient(config) if config is not None else CoolapkClient()
        _client = replacement
        _client_fp = fp
        current = replacement
    # 旧实例交给退休队列：正在用它的并发请求还能把这一轮跑完
    _retire(old)
    return current


def _dump(obj: Any, _seen: frozenset = frozenset()) -> Any:
    """递归 dump：模型转 dict，list/dict 逐项处理，其余原样。

    _seen 按对象 id 防循环引用（响应来自 resp.json() 理论上无环，防御性兜底）。
    """
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", exclude_defaults=True)
    if isinstance(obj, list):
        return [_dump(item, _seen) for item in obj]
    if isinstance(obj, dict):
        if id(obj) in _seen:
            return "…(循环引用)"
        seen = _seen | {id(obj)}
        return {k: _dump(v, seen) for k, v in obj.items()}
    return obj


def _json(data: Any, whitelist: str | None = None) -> str:
    """dump 后可选按白名单裁剪。"""
    if data is None:
        return json.dumps({"error": "无数据"}, ensure_ascii=False)
    data = _dump(data)
    if whitelist:
        data = apply_tool_whitelist(whitelist, data)
    return json.dumps(data, ensure_ascii=False)


# ---- 图片中转（/img）辅助：缩略 URL 生成与带大小上限的上游取图 ----


def _thumb_variant(url: str) -> str | None:
    """带 @WxH 尺寸后缀的 jpg 返回酷安服务端缩略地址（.m.jpg），否则 None。

    只改 URL 的 path 段：原实现 url + ".m.jpg" 会把后缀拼到 query 之后
    （xxx@1080x1080.jpg?x=1 → xxx@1080x1080.jpg?x=1.m.jpg），带查询串的图
    一定会缩略失败。使用 urlsplit/urlunsplit 保证 scheme、netloc、query、fragment 原样保留。
    """
    parts = urlsplit(url)
    path = parts.path
    if not path.endswith(".jpg") or "@" not in path:
        return None
    return urlunsplit(
        (parts.scheme, parts.netloc, f"{path}.m.jpg", parts.query, parts.fragment)
    )


async def _fetch_upstream_image(
    client: httpx.AsyncClient, target: str, headers: dict[str, str], method: str = "GET"
) -> tuple[int, bytes, str, dict | None, int]:
    """带大小上限地拉取上游图片。

    返回 (状态码, 响应体, content-type, 结构化错误体, 错误状态码)。
    出错时响应体为空、结构化错误体非 None，由调用方直接转成 JSONResponse。

    安全点：上游可用 30x 把我们引到任意地址（SSRF）。因此不交给 httpx 自动跟随
    （那是「先连上内网地址、再看 host 决定要不要把结果给出去」——连接已经发出去了，
    等于送出盲 SSRF/端口探测原语），改为手工最多跟 3 跳，每一跳发请求前先验 host
    与端口。method="HEAD" 时只取状态与 content-type，不读正文（探活不该触发整图下载）。
    """
    current = target
    for _hop in range(4):
        parts = urlsplit(current)
        host_now = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or host_now not in IMAGE_HOSTS:
            return (
                0,
                b"",
                "",
                {
                    "error": "forbidden_host",
                    "detail": f"域名 {host_now or '?'} 不在酷安图片域名白名单",
                },
                403,
            )
        if parts.port not in (None, 80, 443):
            return (
                0,
                b"",
                "",
                {"error": "forbidden_host", "detail": "只允许 80/443 端口"},
                403,
            )
        async with client.stream(
            method, current, headers=headers, follow_redirects=False
        ) as resp:
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location") or ""
                if not location:
                    return resp.status_code, b"", "", None, 0
                # 下一跳统一在循环顶部校验，不在这里「边跳边信」
                current = str(httpx.URL(current).join(location))
                continue
            if method == "HEAD":
                # HEAD 只要状态码与类型：大图探活时不该把 15MB 正文读进来再丢掉
                return resp.status_code, b"", resp.headers.get("content-type", ""), None, 0
            return await _read_image_body(resp)
    return (
        0,
        b"",
        "",
        {"error": "too_many_redirects", "detail": "上游跳转超过 3 次"},
        403,
    )


async def _read_image_body(resp: "httpx.Response") -> tuple[int, bytes, str, dict | None, int]:
    """读取图片响应体：声明长度与流式累计两道 15MB 闸门。"""
    if resp.status_code != 200:
        return resp.status_code, b"", "", None, 0
    declared = (resp.headers.get("content-length") or "").strip()
    if declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
        return (
            0,
            b"",
            "",
            {
                "error": "image_too_large",
                "detail": f"上游声明 {int(declared)} 字节，超过 15MB 上限",
            },
            413,
        )
    buf = bytearray()
    async for chunk in resp.aiter_bytes():
        buf.extend(chunk)
        if len(buf) > MAX_IMAGE_BYTES:
            return (
                0,
                b"",
                "",
                {"error": "image_too_large", "detail": "图片超过 15MB 上限"},
                413,
            )
    return 200, bytes(buf), resp.headers.get("content-type", "image/jpeg"), None, 0


def _parse_feed_list(raw: Any) -> list[FeedModel]:
    """解析动态列表：只收真实动态，运营卡片不再变成 id=null 的幽灵条目。

    酷安列表接口会混入 entityType='card' 的运营卡片（「优质图文」等），此前一律
    过 FeedModel.from_api，输出里就出现只有 images/title、id=null 的条目，还把
    fetched 撑大、把 has_more 顶成 true；卡片内嵌的 feed 子项照样保留，与 CLI 的
    _parse_feeds 同口径。
    """
    if not isinstance(raw, list):
        return []
    items: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        if entry.get("entityType") == "card":
            items.extend(
                sub
                for sub in (entry.get("entities") or [])
                if isinstance(sub, dict) and sub.get("entityType") == "feed"
            )
            continue
        items.append(entry)
    return [FeedModel.from_api(item) for item in items]


def _page_fill(raw: Any) -> int:
    """本页原始条目数：has_more 按「接口这一页是否填满」判断，卡片也算占位。

    不能用解析后的条数：卡片被滤掉、卡片内嵌套动态被展开，两个方向都会让「本页
    几条」偏离接口页大小，末页判定随之失真。
    """
    return len(raw) if isinstance(raw, list) else 0


# 图片工具必须显式声明 structured_output=False：FastMCP 见到 list[Image] 会尝试用
# pydantic 建结构化返回 schema，而 mcp 的 Image 不是可建模类型，导入期直接抛
# PydanticSchemaGenerationError；显式关闭后运行时由 _convert_to_content 把每个
# Image 转成一块 ImageContent —— 正好是多张图片结果。
_MAX_IMAGES = 9


def _paging_meta(page: int, filled: int, size: int | None = None) -> dict[str, Any]:
    """列表工具的分页提示：has_more + next_page。

    为什么要它：此前列表工具只回 page/fetched，调用方只能盲猜 page+1，翻过头
    拿到空列表也判断不出「到底了」；而 feed_detail/author_digest 早就有
    truncated/next_page，两套口径让 agent 无法用同一套逻辑决定要不要继续翻。

    判定口径是「本页是否填满」：酷安末页通常不满一页，填满即认为可能还有下一页
    （多翻一页最多拿到空列表，不会漏数据）。filled 传解析后的条数，绝不能传
    关键词过滤后的条数，否则「过滤后剩 3 条、后面还有」会被误判成末页。
    """
    size = int(CoolapkClient.PAGE_SIZE if size is None else size)
    if size <= 0:
        # 显式关掉分页提示（COOLAPK_PAGE_SIZE=0 之类）：别再 max(1) 把它变成 1，
        # 那会让 has_more 恒 true，调用方永远翻不到底
        return {"has_more": False, "next_page": None}
    has_more = filled >= size
    return {"has_more": has_more, "next_page": page + 1 if has_more else None}


def _search_empty_hint(meta: dict, keyword: str) -> str:
    """空搜索结果的解释：区分「酷安没有」「被过滤掉」「只剩广告位」三种原因。"""
    raw_count = int(meta.get("raw_count") or 0)
    filtered = int(meta.get("keyword_filtered") or 0)
    if filtered:
        return (
            f"酷安返回 {raw_count} 条，其中 {filtered} 条因关键词严格匹配被过滤；"
            f"可设 strict=false 查看原始结果，或把「{keyword}」拆成更短的词、"
            f"先用 coolapk_suggest 查规范用词"
        )
    if raw_count:
        return (
            f"酷安返回 {raw_count} 条但没有可用帖子（多为卡片/广告位）；"
            f"这与 strict 无关（改 strict=false 结果相同），请换页码或换个更通用的关键词"
        )
    return f"酷安没有搜到「{keyword}」：可用 coolapk_suggest 查联想词，或换更通用的说法"


def _normalize_keyword_mode(value: str) -> str:
    """keyword_mode 只认 any/all，其余（含大小写、空格）一律回落 any。

    静默降级要能看见：调用方把生效值回显进 payload，否则传了 all 却按 any
    过滤时，agent 会以为「只有这些命中」。
    """
    return "all" if str(value or "").strip().lower() == "all" else "any"


def _prepare_feeds(
    raw: Any, keyword: str = "", sort: str = "default", keyword_mode: str = "any"
) -> tuple[list[dict], int]:
    """把原始动态列表加工成带质量分的输出：过模型 → 关键词过滤 → 打分 → 排序。

    keyword 空格分隔多词：keyword_mode=any（默认）任一命中标题或正文即保留；
    all 要求全部命中，用于精确圈定同一主题。注意与 coolapk_search 的 strict
    并不等价：这里是「标题+正文的子串匹配」，与 search 共用去空白/#/_ 的归一化，
    但不匹配作者名，所以词只出现在作者名时会漏。
    sort=hot 按 post_score（点赞+评论×3+转发×2）降序，latest 按发布时间降序。
    """
    keyword_mode = _normalize_keyword_mode(keyword_mode)
    if not isinstance(raw, list):
        return [], 0
    items: list[dict] = []
    haystacks: list[str] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        items.append(_dump(FeedModel.from_api(entry)))
        # 关键词在原始文本上匹配：FeedModel 会把正文截到 FEED_CONTENT_MAX_CHARS，
        # 按截断后的 content 匹配时，长文末尾出现的关键词会被漏掉（实测 2100 字
        # 正文末尾命中 0 条，关键词挪到开头就命中 1 条）
        haystacks.append(
            postfilter.normalize_text(
                f"{entry.get('title') or ''} {entry.get('message') or ''}"
            )
        )
    total = len(items)
    words = [w for w in (postfilter.normalize_text(x) for x in (keyword or "").split()) if w]
    if words:
        def _hit(haystack: str) -> bool:
            hits = [w in haystack for w in words]
            return all(hits) if keyword_mode == "all" else any(hits)

        items = [it for it, text in zip(items, haystacks, strict=True) if _hit(text)]
    for it in items:
        scoring.annotate_post(it)
    if sort == "hot":
        items.sort(key=lambda x: x.get("post_score") or 0, reverse=True)
    elif sort == "latest":
        items.sort(key=lambda x: str(x.get("dateline") or ""), reverse=True)
    return items, total


def _error_json(
    message: str,
    error_type: str,
    code: int = -1,
    retry_after: float | None = None,
    retryable: bool = False,
) -> str:
    """统一的工具错误输出结构：error / error_type / code（可选 retry_after、retryable）。

    retry_after 只在有明确建议等待时间时出现（风控冷却、服务端 Retry-After）：
    单位秒、保留一位小数。retryable 仅在 True 时出现，告诉 agent「原样重试是否有
    意义」——network/transient/rate_limit 为真，auth_expired/risk/api 为假。
    两个键都只在有值时才输出，避免给简单错误塞一堆 null。
    """
    payload: dict[str, Any] = {
        "error": message,
        "error_type": error_type,
        "code": code,
    }
    if retry_after is not None:
        payload["retry_after"] = round(float(retry_after), 1)
    if retryable:
        payload["retryable"] = True
    return json.dumps(payload, ensure_ascii=False)


def _catch_api_error(fn):
    """兜底封装：任何异常都转成结构化 JSON，工具永不向上抛。

    - CoolapkError → 沿用原有 error_type/code（403 登录失效 vs 1004 风控可分派）
    - asyncio.TimeoutError / httpx.HTTPError → network（超时、连接失败、协议错误）
    - 其余 Exception → internal，堆栈只进服务端日志，不回传客户端
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except CoolapkError as e:
            if e.kind == "auth_expired":
                # 登录态失效：关闭并丢弃旧 client（其构造时快照的登录态已过期），
                # 下次调用重建并重新读 config——桌面端重登后直接重试即可生效
                logger.info("检测到登录态失效，关闭并丢弃旧 client")
                await _reset_client()
            return _error_json(
                e.message, e.kind, e.error_code, e.retry_after, e.retryable
            )
        except (asyncio.TimeoutError, httpx.HTTPError) as exc:
            logger.warning("工具 %s 网络异常：%s", getattr(fn, "__name__", "?"), exc)
            # retryable=True 与 README 的「network 可重试」保持一致：只有显式
            # 带 retryable 的客户端才敢重试，缺了这个键等于劝退重试
            return _error_json(
                f"网络请求失败（{type(exc).__name__}），可稍后重试",
                "network",
                retryable=True,
            )
        except Exception as exc:  # noqa: BLE001 —— 兜底，异常不向上抛给 MCP 客户端
            logger.exception("工具 %s 未预期异常：%s", getattr(fn, "__name__", "?"), exc)
            # 只回传异常类名，堆栈与内部细节仅记录在服务端日志
            return _error_json(
                f"服务内部错误（{type(exc).__name__}），详情见服务端日志", "internal"
            )

    return wrapper


@mcp.tool()
@_catch_api_error
async def coolapk_search(
    keyword: str,
    type: str = "feed",
    page: int = 1,
    strict: bool = True,
    sort: str = "hot",
) -> str:
    """搜索酷安社区内容（自动去重 + 严格关键词过滤 + 疑似广告标记）。

    Args:
        keyword: 搜索关键词；多个词用空格分隔，strict 模式下要求全部命中
        type: 搜索类型，可选 feed(帖子) user(用户) feedTopic(话题) app(应用)
        page: 页码
        strict: 严格过滤（默认开）：归一化后每个关键词都必须出现在标题/正文/作者名里，
            可滤掉酷安搜索混入的不相关结果与机型名单广告；关闭则返回原始结果
        sort: 帖子排序，hot(热门，默认)/reply(回复数)/default(综合)。
            强烈建议保持默认 hot：酷安默认排序混入大量低互动点评，
            实测 hot 直接返回高赞干货帖；结果带 like_num/reply_num 可再筛选
    """
    page = max(1, int(page or 1))
    client = await _get_client()
    data = await client.search(keyword, type, page, sort=sort)
    if not isinstance(data, list):
        return _json(data)
    # feed 走严格过滤，user/feedTopic/app 仅剔除非实体卡片
    kept, meta = postfilter.filter_search_results(
        data, keyword, strict=strict if type == "feed" else False
    )
    results = postfilter.convert_search_items(kept)
    # results 已是 dump 后的 dict 列表，直接按条目类型分发白名单
    results = apply_tool_whitelist("search", results)
    payload: dict[str, Any] = {
        "page": page,
        # 用原始条数判断下一页：结果被 strict 过滤后变少，按结果数判断会把
        # 「过滤后只剩 3 条、后面还有 30 条」误判成末页
        # 搜索接口每页约 20 条（不是 PAGE_SIZE=10）：按 PAGE_SIZE 判会把真实末页
        # 也标成 has_more=true，用户白翻一页空页
        **_paging_meta(
            page, int(meta.get("raw_count") or 0), size=CoolapkClient.SEARCH_PAGE_SIZE
        ),
        "counts": meta,
        "results": results,
    }
    if not results:
        # 空结果必须解释原因：被关键词全滤掉时若只说「没搜到」，用户会以为
        # 酷安社区没有这个内容，而实际是几十条被 strict 判掉了
        payload["hint"] = _search_empty_hint(meta, keyword)
    return json.dumps(payload, ensure_ascii=False)


@mcp.tool()
@_catch_api_error
async def coolapk_feed_detail(
    feed_id: str | int,
    with_replies: bool = True,
    reply_page: int = 1,
    all_replies: bool = False,
    comment_keyword: str | None = None,
    comments_focus: str = "default",
    with_history: bool = False,
    expand_sub_replies: bool = False,
    focus_page: int = 1,
) -> str:
    """获取酷安帖子的完整详情和回复（详情接口被风控时自动回落）。

    工具帖找资料建议用 comments_focus=smart：一次拿到「正文 + 楼主补充回复 +
    高赞反馈」，获取方式与答疑多在楼主回复里（每个来源最多翻 5 页）。

    Args:
        feed_id: 帖子 ID，也可直接粘贴酷安分享链接或整段分享文案
            （如 https://www.coolapk.com/feed/1234567890），自动解析出 ID
        with_replies: 是否包含回复列表，默认是
        reply_page: 回复页码（all_replies / comment_keyword / comments_focus 非 default 时忽略）
        all_replies: 拉全部一级评论（自动翻页，含内联楼中楼），适合完整舆情/评论分析
        comment_keyword: 按关键词检索评论和楼中楼（最多翻 COOLAPK_MAX_REPLY_PAGES 页，
            默认 30；空格分隔多词=任一命中；命中条目带 comment_match 字段，
            楼中楼按上下文保留，统计见 comment_search；撞上限时 replies_truncated=True）
        comments_focus: 评论聚焦模式：default（默认时间序一页）/ smart（楼主回复
            + 热门评论，各最多 5 页，找工具帖资料首选）/ author（只看楼主回复）/
            hot（只看热门评论）/ all（全部一级评论，等同 all_replies）。
            楼主回复与热门评论均按页收集（某页不足 10 条即末页，最多 5 页），
            是否取全看返回的 focus_meta.truncated / pages_fetched / next_page /
            budget（两个来源合计的请求数与耗时）
        with_history: 附带正文修改历史（作者每次编辑的快照，看版本演进）
        expand_sub_replies: 深度展开楼中楼（对回复数多的父评论补全子回复，
            楼主答疑常在楼中楼深处；会额外发请求，只在需要时开）。
            有父评论没取全时返回 sub_replies_truncated=True
        focus_page: 焦点评论起始页（消费上一次的 focus_meta.next_page 用；
            只在 comments_focus 为 smart/author/hot 时生效）
    """
    # 用户贴过来的通常是分享链接/整段文案，让模型自己抠数字既多一步又容易抠错
    resolved_id = parse_feed_id(feed_id)
    if resolved_id is None:
        return _error_json(
            "无法从 feed_id 解析出帖子 ID：可直接传数字，或粘贴酷安分享链接/整段"
            "分享文案（如 https://www.coolapk.com/feed/1234567890）",
            "api",
        )
    feed_id = resolved_id
    client = await _get_client()
    focus = (comments_focus or "default").strip().lower()
    if focus not in ("default", "smart", "author", "hot", "all"):
        focus = "default"

    # all_replies 与 focus=all 等价
    if focus == "all":
        all_replies = True

    focus_active = focus in ("smart", "author", "hot")
    want_replies = with_replies or bool(comment_keyword)
    smart = await client.get_feed_detail_smart(
        feed_id,
        # focus 活跃时先不预拉默认回复：collect_focus 会整份替换 replies，
        # 预拉的那一页在下游没人看，却要多排一次 1.2s 限速窗口；
        # 真取不到时在下面回拉一页兜底，语义不变而常规路径少一次请求。
        with_replies=want_replies and not focus_active,
        reply_page=reply_page,
        all_replies=all_replies or bool(comment_keyword),
    )
    detail = FeedDetail.from_api(
        smart["data"],
        smart["replies"] if isinstance(smart["replies"], list) else None,
        fetch_source=smart["source"],
    )
    # all_replies=True 时 client 返回 FetchResult，分页元数据在列表对象上；
    # FeedDetail.from_api 只保留回复条目，这里把截断状态单独透传到模型，
    # 避免 30 页上限/预算耗尽时输出看起来像"全量已取完"。
    raw_replies = smart.get("replies")
    if raw_replies is not None:
        detail.replies_truncated = bool(getattr(raw_replies, "truncated", False))
        detail.replies_next_page = getattr(raw_replies, "next_page", None)

    # ---- 评论聚焦模式：单独拉楼主评论 / 热门评论替换默认列表 ----
    # 翻页、去重、预算与截断判定都在 focus.collect_focus 里（与 CLI 共用一份实现）
    focus_meta: dict | None = None
    if focus_active:
        merged, focus_meta = await collect_focus(
            client, feed_id, focus, start_page=max(1, int(focus_page or 1))
        )
        if focus_meta is not None:
            focus_meta["total_replynum"] = smart["data"].get("replynum") or 0
        if merged:
            detail.replies = [ReplyModel.from_api(r) for r in merged]
        elif want_replies:
            # 聚焦两路都失败或为空：回拉一页默认回复，别让调用方拿到空评论区
            try:
                fallback = await client.get_feed_replies(feed_id, reply_page)
            except CoolapkError:
                fallback = None
            if fallback:
                detail.replies = [ReplyModel.from_api(r) for r in fallback]
                focus_meta = {**(focus_meta or {}), "fallback": "default_replies"}
    if focus_meta is not None:
        detail.comments_focus = focus_meta

    # ---- 修改历史 ----
    if with_history:
        try:
            hist = await client.get_feed_history(feed_id)
            if isinstance(hist, list) and hist:
                detail.edit_history = [
                    {
                        "id": h.get("id", 0),
                        "content": _strip_html_keep_links(h.get("message", "")),
                        "dateline": _format_time(h.get("lastupdate", 0)),
                    }
                    for h in hist
                    if isinstance(h, dict)
                ]
        except CoolapkError:
            detail.edit_history = []

    # ---- 楼中楼深度展开（楼主在楼中楼的答疑 + 高赞父评论） ----
    if expand_sub_replies and detail.replies:
        # 每条父评论最多补 3 页；前 12 条父评论共用一份 24 次/20s 的预算，
        # 预算耗尽就停在已展开的部分，不让附加请求拖长整次工具调用。
        budget = Budget(max_requests=24, deadline=20.0)
        expanded = 0
        sub_truncated = False
        for i, r in enumerate(detail.replies[:12]):
            need = (r.reply_count or 0) - len(r.sub_replies or [])
            if need <= 0:
                continue
            try:
                subs = await client.get_sub_replies_paged(
                    r.id, 1, max_pages=3, budget=budget
                )
            except BudgetExceeded:
                break
            except CoolapkError:
                continue
            if getattr(subs, "truncated", False):
                sub_truncated = True
            if subs:
                detail.replies[i].sub_replies = [
                    ReplyModel.from_api(sr) for sr in subs
                ]
                expanded += len(subs)
        if expanded:
            detail.sub_replies_expanded = expanded
        if sub_truncated:
            # 有父评论的楼中楼撞到页数上限没取全，上层据此决定是否继续深挖
            detail.sub_replies_truncated = True

    if comment_keyword:
        # 关键词检索必须发生在条数截断之前：一条 300 回复的帖子若先截到前 60 条
        # 再检索，命中落在第 70 条的评论会被报成「没有」，而用户问的正是
        # 「有没有人提到 X」——这是会误导判断的假阴性。
        dumped = detail.model_dump(mode="json", exclude_defaults=True)
        dumped = postfilter.filter_comments(dumped, comment_keyword)
        matched = dumped.get("replies") or []
        if REPLY_MAX_ITEMS > 0 and len(matched) > REPLY_MAX_ITEMS:
            # 命中数超过输出上限时按上限裁剪（replies_omitted = 命中但没列出的条数）
            dumped["replies"] = matched[:REPLY_MAX_ITEMS]
            dumped["replies_omitted"] = len(matched) - REPLY_MAX_ITEMS
            dumped["replies_truncated"] = True
        if dumped.get("replies_truncated"):
            # 评论检索本身没有翻页逻辑：截断可能来自上游 all_replies 没扫完，
            # 也可能来自命中数超过输出上限。不标出来会让 agent 误以为
            # "关键词只命中这些"，实际是没扫完/没列全。
            stats = dumped.setdefault("comment_search", {})
            stats["truncated"] = True
            if detail.replies_next_page is not None:
                stats["next_page"] = detail.replies_next_page
        # 与默认分支同源：_json 内部做 dump + 白名单裁剪
        return _json(dumped, whitelist="feed_detail")

    # 回复条数上限：all_replies 一条帖子能翻 300 条，整份塞进上下文会把后续
    # 对话挤掉。保留前 N 条并标出省略条数（replies_omitted），比静默少给要诚实
    detail = cap_replies(detail)
    return _json(detail, whitelist="feed_detail")


@mcp.tool()
@_catch_api_error
async def coolapk_user_profile(uid: int) -> str:
    """查看酷安用户资料。

    Args:
        uid: 用户UID
    """
    client = await _get_client()
    data = await client.get_user_space(uid)
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        return _json(UserModel.from_api(data), whitelist="user_profile")
    return _json(data)


@mcp.tool()
@_catch_api_error
async def coolapk_user_feeds(
    uid: int,
    page: int = 1,
    pages: int = 1,
    keyword: str = "",
    sort: str = "default",
    keyword_mode: str = "any",
) -> str:
    """查看酷安用户发布的帖子（支持多页聚合、关键词过滤与热度排序）。

    Args:
        uid: 用户UID
        page: 起始页码（pages>1 时从该页开始连续抓取）
        pages: 连续抓取页数 1-5，默认 1；想看某作者的历史干货时设 2-3
        keyword: 只保留标题或正文含该词的帖子（空格分隔多词=任一命中）
        sort: 排序，default(接口原始顺序) / hot(互动热度) / latest(发布时间)
        keyword_mode: any(默认，任一命中) / all(全部命中，用于精确圈定同一主题)
    """
    client = await _get_client()
    start = max(1, int(page or 1))
    wanted = max(1, min(int(pages or 1), 5))
    raw: Any
    if wanted > 1:
        # 多页聚合走统一预算：页数即请求上限，另留 2 次余量（重试/补偿页），
        # 超限时 client 返回已抓到的部分并把 truncated/next_page 标出来。
        budget = Budget(max_requests=wanted + 2, deadline=20.0)
        raw = await client.get_user_feeds_pages(
            uid, pages=wanted, start_page=start, budget=budget
        )
    else:
        raw = await client.get_user_feeds(uid, start)
    if not isinstance(raw, list):
        return _json(raw)
    mode = _normalize_keyword_mode(keyword_mode)
    items, total = _prepare_feeds(raw, keyword, sort, mode)
    return _json(
        {
            "uid": uid,
            "pages": wanted,
            "fetched": total,
            "matched": len(items),
            "sort": sort,
            "keyword": keyword or None,
            "keyword_mode": mode,
            # 翻页截断：truncated=True 说明还有页没取（预算或页数上限）；
            # next_page 是续抓起点，未截断时为 null
            "truncated": bool(getattr(raw, "truncated", False)),
            "next_page": getattr(raw, "next_page", None),
            "feeds": items,
        },
        whitelist="user_feeds",
    )


@mcp.tool()
@_catch_api_error
async def coolapk_author(uid: int) -> str:
    """查看酷安作者的画像与权威度评分（判断「这人是不是该领域大佬」）。

    返回基础资料 + author_tier(top/expert/active/normal)、is_expert、
    fans_per_feed(精发度)、expertise(从 bio 推断的专长领域)。
    搜到高赞帖后先看作者 tier，再决定要不要用 coolapk_author_digest 深挖他的其他帖。

    Args:
        uid: 用户UID
    """
    client = await _get_client()
    data = await client.get_user_space(uid)
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        return _json(data)
    profile = _dump(UserModel.from_api(data))
    profile.update(scoring.summarize_author(profile))
    return _json(profile, whitelist="author")


@mcp.tool()
@_catch_api_error
async def coolapk_author_digest(
    uid: int, keyword: str = "", pages: int = 2, top: int = 10, keyword_mode: str = "any"
) -> str:
    """深挖某作者：一次调用拿到他在指定主题下的全部高赞帖（按热度排序）。

    适合发现领域大佬（author_tier=top/expert）后，一次性抓出他关于该主题的
    完整攻略链（例如「莫离然然」的解锁工具 v1.4→v2.2 全系教程）。
    内部按 id 去重（置顶帖会跨页重复），关键词空格分隔多词=任一命中。

    Args:
        uid: 作者UID
        keyword: 主题关键词（空格分隔多词=任一命中），留空返回全部帖子
        pages: 抓取页数 1-5，默认 2（约 40 条）
        top: 返回条数上限，默认 10，最多 50
        keyword_mode: any(默认，任一命中) / all(全部命中，避免「解锁」类泛词召回)
    """
    client = await _get_client()
    wanted = max(1, min(int(pages or 1), 5))
    # 与 coolapk_user_feeds 同一预算口径：页数上限 + 2 次余量
    budget = Budget(max_requests=wanted + 2, deadline=20.0)
    # 动态列表与作者资料互不依赖：并发发出，省掉一次 1.2s 限速等待
    results = await asyncio.gather(
        client.get_user_feeds_pages(uid, pages=wanted, budget=budget),
        client.get_user_space(uid),
        return_exceptions=True,
    )
    raw: Any = results[0]
    space: Any = results[1]
    if isinstance(raw, BaseException):
        raise raw
    author_error: str | None = None
    if isinstance(space, BaseException):
        # 资料页失败不该拖垮帖子列表，降级为提示
        author_error = f"{type(space).__name__}: {space}"[:200]
        space = {}
    mode = _normalize_keyword_mode(keyword_mode)
    posts, total = _prepare_feeds(raw, keyword, "hot", mode)
    limit = max(1, min(int(top or 10), 50))
    posts = posts[:limit]
    if isinstance(space, list) and space:
        space = space[0]
    author: dict = {}
    if isinstance(space, dict):
        author = _dump(UserModel.from_api(space))
        author.update(
            scoring.summarize_author(
                {
                    **author,
                    "posts_texts": [
                        f"{p.get('title') or ''} {p.get('content') or ''}" for p in posts
                    ],
                }
            )
        )
    return _json(
        {
            "uid": uid,
            "username": author.get("username"),
            "author": author,
            **(({"author_error": author_error}) if author_error else {}),
            "keyword": keyword or None,
            "keyword_mode": mode,
            "pages": wanted,
            "fetched": total,
            "matched": len(posts),
            # 帖子列表撞到页数上限/预算时 truncated=True，next_page 为续抓起点
            "truncated": bool(getattr(raw, "truncated", False)),
            "next_page": getattr(raw, "next_page", None),
            "posts": posts,
        },
        whitelist="author_digest",
    )



@mcp.tool()
@_catch_api_error
async def coolapk_home(tab: str = "recommend", page: int = 1) -> str:
    """获取酷安首页动态（推荐 / 关注 / 热门 / 最新）。

    返回 {"tab", "page", "fetched", "has_more", "next_page", "feeds"}：tab 回显
    实际分栏（未知取值按 recommend 处理），feeds 为动态列表。

    Args:
        tab: recommend(推荐，/v6/main/indexV8 算法推荐) follow(关注，只看已关注
            的人) hot(热门) latest(最新)
        page: 页码，默认 1
    """
    client = await _get_client()
    tab = (tab or "").strip().lower()
    page = max(1, int(page or 1))
    # 语义修正：此前 recommend 走 V9_HOME_TAB_FOLLOW（关注流），关注少的人会
    # 拿到几条甚至空列表，看起来像工具坏了；真推荐是 main/indexV8。关注流保留
    # 为独立的 follow 分栏，两个语义不再混用一个名字。
    # 未知取值静默回落会让调用方以为参数生效了：回显时用 tab_fallback 标出来
    tab_fallback = tab not in ("recommend", "follow", "dynamic", "关注", "hot", "latest")
    if tab in ("follow", "dynamic", "关注"):
        tab = "follow"
        data = await client.get_home_feeds(page)
    elif tab == "hot":
        data = await client.get_hot_feeds(page)
    elif tab == "latest":
        data = await client.get_latest_feeds(page)
    else:
        tab = "recommend"
        data = await client.get_main_feeds(page)
    if isinstance(data, list):
        feeds = _parse_feed_list(data)
        return _json(
            {
                "tab": tab,
                "page": page,
                "fetched": len(feeds),
                "feeds": feeds,
                **({"tab_fallback": True} if tab_fallback else {}),
                **_paging_meta(page, _page_fill(data)),
            },
            whitelist="home",
        )
    return _json(data)


def _resolve_board_arg(board: str) -> tuple[str, str] | None:
    """把用户/agent 给的板块标识解析成 (board_id, url)。

    与 client 里的宽松回落不同：这里要求能落到已知板块或显式 page_name/url，
    否则返回 None 让工具报「未知板块 + 可用列表」，而不是拿乱串去请求接口。
    """
    name = (board or "").strip()
    if not name:
        return None
    if name in BOARD_URLS:
        return name, BOARD_URLS[name][1]
    # 中文板块名（数码/手机/二手交易…）此前未支持：README 与工具描述都写了
    # 「支持板块 id 或中文名」，但这里只认英文 id 与 page_name，传中文会落到
    # 「未知板块」。按标题精确匹配一次即可（21 项，开销可忽略）。
    for board_id, (title, url) in BOARD_URLS.items():
        if name == title:
            return board_id, url
    for board_id, (_title, url) in BOARD_URLS.items():
        if "url=" in url and url.split("url=", 1)[1] == name:
            return board_id, url
    # 显式完整 url / page 路径（含 / 或 #）：原样透传，交给接口判定
    if "/" in name or name.startswith("#"):
        return name, name
    return None


@mcp.tool()
@_catch_api_error
async def coolapk_board(board: str, page: int = 1) -> str:
    """浏览酷安板块（数码、二手、摄影、汽车、好物等 21 个板块）。

    板块动态与首页动态是两套入口：想看「数码区最近在聊什么」「二手区的行情」
    用这个工具，不要用 coolapk_home。板块 ID 见 boards 映射，常用：
    digital(数码) phone(手机) computer(电脑) system(系统ROM) sheying(摄影)
    meihua(美化) car(汽车) waishe(外设) goods(好物) kaixiang(开箱)
    secondhand(二手) secondhand_apple(二手苹果) hot(热榜) news(快讯) video(视频)
    wenda(问答) topic(话题) headline(头条) xianliao(闲聊) coolpic(酷图)
    newphone(新机)。

    Args:
        board: 板块 ID（如 digital）、中文名（如 手机/数码/二手交易）或
            page_name（如 V10_DIGITAL_HOME）
        page: 页码，默认 1
    """
    resolved = _resolve_board_arg(board)
    if resolved is None:
        return _error_json(
            f"未知板块: {board}（可用板块 id：{'/'.join(sorted(BOARD_URLS))}；"
            f"也可直接传中文名，如 数码/手机/二手交易）",
            "api",
        )
    board_id, _url = resolved
    page = max(1, int(page or 1))
    client = await _get_client()
    raw = await client.get_board_feeds(board_id, page)
    if isinstance(raw, list):
        feeds = _parse_feed_list(raw)
        return _json(
            {
                "board": board_id,
                "page": page,
                "fetched": len(feeds),
                "feeds": feeds,
                **_paging_meta(page, _page_fill(raw)),
            },
            whitelist="board",
        )
    return _json(raw)


@mcp.tool()
@_catch_api_error
async def coolapk_secondhand(brand: int = 0, page: int = 1) -> str:
    """二手交易区动态（想了解某机型二手行情、看出售帖时用）。

    Args:
        brand: 品牌 ID，0 表示全部；1005=苹果（其余品牌 ID 可先用 coolapk_search
            搜到手机话题后按需试）
        page: 页码，默认 1
    """
    page = max(1, int(page or 1))
    brand_id = int(brand or 0)
    client = await _get_client()
    raw = await client.get_secondhand_feeds(brand_id or None, page)
    if isinstance(raw, list):
        feeds = _parse_feed_list(raw)
        return _json(
            {
                "brand": brand_id or None,
                "page": page,
                "fetched": len(feeds),
                "feeds": feeds,
                **_paging_meta(page, _page_fill(raw)),
            },
            whitelist="secondhand",
        )
    return _json(raw)


@mcp.tool()
@_catch_api_error
async def coolapk_topic(
    tag: str, with_feeds: bool = False, page: int = 1, hot_first: bool = True
) -> str:
    """查看酷安话题详情或话题下的帖子。

    Args:
        tag: 话题标签名
        with_feeds: 是否获取话题下的帖子列表
        page: 帖子页码
        hot_first: 话题帖子按热度排序（点赞+评论*3，默认开）；
            关闭则保持酷安默认顺序（时间流，多为最新低互动帖）
    """
    client = await _get_client()
    result: dict[str, Any] = {}
    # 详情与动态分开降级：话题详情接口被风控时，动态列表往往还能取到，
    # 此前一个失败就整次失败，等于白丢一份可用数据
    try:
        detail = await client.get_topic_detail(tag)
    except CoolapkError as exc:
        detail = None
        result["topic_error"] = str(exc)[:200]
    if isinstance(detail, list) and detail:
        detail = detail[0]
    if isinstance(detail, dict):
        result["topic"] = TopicModel.from_api(detail)

    if with_feeds:
        try:
            feeds = await client.get_topic_feeds(tag, page)
        except CoolapkError as exc:
            feeds = None
            result["feeds_error"] = str(exc)[:200]
        if isinstance(feeds, list):
            parsed = _parse_feed_list(feeds)
            if hot_first:
                # 话题动态服务端不支持排序参数（实测 listType/sort 均无效），
                # 客户端按热度重排：like + reply*3（评论权重更高，对齐
                # 酷安官方热门评论的互动口径），纯水动态沉底
                parsed.sort(
                    key=lambda f: f.like_num + f.reply_num * 3,
                    reverse=True,
                )
            result["feeds"] = parsed
    return _json(result, whitelist="topic")


@mcp.tool()
@_catch_api_error
async def coolapk_app_detail(package_name: str) -> str:
    """查看酷安应用详情（包名 → 应用名、简介、关注数、下载量、版本）。

    搜到应用相关动态后，用包名查应用本身：判断「这是不是同一个 App」
    「现在的最新版本号」比只看帖子标题可靠。

    Args:
        package_name: 应用包名（如 com.coolapk.market）
    """
    pkg = (package_name or "").strip()
    if not pkg:
        return _error_json("package_name 不能为空", "api")
    client = await _get_client()
    data: Any = await client.get_app_detail(pkg)
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return _json(data)
    return _json(_dump(AppModel.from_api(data)), whitelist="app_detail")


@mcp.tool()
@_catch_api_error
async def coolapk_notify(ntype: str = "message", page: int = 1, with_count: bool = False) -> str:
    """查看通知（@我的、评论、点赞、关注、私信）。

    适合「有人回复我了吗」「私信里说了什么」这类需要个人时间线的场景；
    只读，不会标记已读或回复。

    Args:
        ntype: 通知类型，可选 atMeMeFeed(@我的动态) atComment(@我的评论)
            comment(评论) feedLike(点赞) contactsFollow(关注) message(私信)，
            默认 message
        page: 页码，默认 1
        with_count: 是否附带各类型未读/总数统计（多一次请求）
    """
    kind = (ntype or "message").strip()
    if kind not in NOTIFY_TYPES:
        return _error_json(
            f"未知通知类型: {kind}（可用：{'/'.join(NOTIFY_TYPES)}）",
            "api",
        )
    page = max(1, int(page or 1))
    client = await _get_client()
    raw = await client.get_notifications(kind, page)
    items = raw if isinstance(raw, list) else []
    # 通知接口返回的是原始 camelCase 条目，dateline 是裸 Unix 秒；其它工具统一
    # 输出 ISO8601+08:00，这里不归一会让 agent 拿到两套时间口径没法比较
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("dateline"):
            item["dateline"] = _format_time(item["dateline"])
        # 其它工具输出统一 snake_case，通知条目来自原始 API，这里把唯一的
        # camelCase 字段（entityType）改名后再进白名单
        if "entityType" in item:
            item["entity_type"] = item.pop("entityType")
    payload: dict[str, Any] = {
        "ntype": kind,
        "page": page,
        "fetched": len(items),
        "items": items,
        **_paging_meta(page, len(items)),
    }
    if with_count:
        try:
            payload["counts"] = await client.get_notification_count()
        except CoolapkError as exc:
            # 统计失败不该拖垮通知本体
            payload["counts"] = {"error": str(exc)[:200]}
    return _json(payload, whitelist="notification")


@mcp.tool()
@_catch_api_error
async def coolapk_suggest(keyword: str) -> str:
    """获取酷安搜索联想词（查询改写积木）。

    输入自然语言或半成品关键词，返回酷安建议的规范搜索词，
    可用于把口语化需求改写成酷安搜索习惯用词后再搜。

    Args:
        keyword: 查询词（可以是口语化长句）
    """
    client = await _get_client()
    data = await client.get_topic_suggest(keyword)
    words: list[str] = []
    if isinstance(data, list):
        for card in data:
            # 联想卡字段是 title（显示词）+ url（searchTab://all?keyword=），
            # 没有 searchWord 键；取 title、剔除「搜索用户：」类前缀卡
            if not isinstance(card, dict):
                continue
            word = str(card.get("title") or "").strip()
            if word and not word.startswith("搜索"):
                words.append(word)
    return json.dumps({"suggests": words[:8]}, ensure_ascii=False)


@mcp.tool()
async def coolapk_login_status() -> str:
    """查看当前酷安登录状态（只读，不触发网络请求）。

    返回 login_mode(登录模式配置)、is_logged_in(是否已登录)、
    uid/username(登录用户)、source(登录态来源 desktop|native|none)、
    device_code(设备码是否已配置)。
    """
    config = AppConfig.load()
    return json.dumps(
        {
            "login_mode": config.login_mode,
            "is_logged_in": config.is_logged_in,
            "uid": config.uid or None,
            "username": config.username or None,
            "source": config.source,
            "device_code": bool(config.device_code),
        },
        ensure_ascii=False,
    )


@mcp.tool(structured_output=False)
@_catch_api_error
async def coolapk_read_image(url: str) -> list[Image]:
    """下载酷安 CDN 图片并作为图片返回（支持一次多张，九宫格帖一次看全）。

    仅允许酷安图片域名：image.coolapk.com / avatar.coolapk.com /
    static.coolapk.com。失败时返回结构化错误，可改用 coolapk_get_url
    获取接口数据，或直接向用户呈现 URL。

    Args:
        url: 一个或多个图片 URL，用逗号、空格或换行分隔，最多 9 张
            （http 自动升级 https）
    """
    # 去重保序：同一张图粘两遍时没必要下载两次（限速窗口很贵）
    urls = list(dict.fromkeys(split_urls(url)))
    if not urls:
        raise CoolapkError("url 不能为空", kind="api")
    if len(urls) > _MAX_IMAGES:
        raise CoolapkError(
            f"一次最多 {_MAX_IMAGES} 张图（收到 {len(urls)} 张）：请分批调用", kind="api"
        )
    # 先把域名/协议一次性校验完再下载：第 5 张才发现域名不对，前 4 张的网络
    # 请求就白发了；这里与 client.download_image 用同一份域名白名单
    for item in urls:
        parts = urlsplit(item.strip())
        if parts.scheme not in ("http", "https") or (
            parts.hostname or ""
        ).lower() not in IMAGE_HOSTS:
            raise CoolapkError(
                f"图片域名不在白名单: {item}（仅允许 {'/'.join(sorted(IMAGE_HOSTS))}）",
                error_code=403,
                kind="api",
            )
    client = await _get_client()
    images: list[Image] = []
    for index, item in enumerate(urls, 1):
        try:
            data, fmt = await client.download_image(item)
        except CoolapkError as exc:
            # 部分失败整体报错并指明是第几张：只返回成功的那几张，调用方会以为
            # 帖子本来就只有这么多图（与真实情况无法区分）
            raise CoolapkError(
                f"第 {index}/{len(urls)} 张下载失败（{item}）：{exc.message}",
                error_code=exc.error_code,
                kind=exc.kind,
            ) from exc
        images.append(Image(data=data, format=fmt))
    return images


@mcp.tool()
async def coolapk_image_link(urls: str, ttl_minutes: int = 120) -> str:
    """把酷安图片地址转成可直接在聊天窗口显示的 https 链接（需远程 HTTP 部署）。

    为什么需要它：酷安 image.coolapk.com 对非酷安客户端返回 HTTP 567（防盗链），
    很多聊天类客户端又没有解码 base64 的能力，只能显示文字链接。此工具把图片
    转成托管在 MCP 服务上的签名链接（/img），链接自带过期时间与 HMAC 签名，
    任何能打开网页的客户端都能直接显示图片。

    仅在远程 HTTP 模式可用：MCP 进程需配置 COOLAPK_PUBLIC_BASE（对外访问前缀，
    例如 https://mcp.example.com）与 COOLAPK_HTTP_TOKEN（用作签名密钥）。

    Args:
        urls: 一个或多个酷安图片 URL，用逗号、空格或换行分隔
        ttl_minutes: 链接有效期（分钟），默认 120，最长 10080（7 天）
    """
    public_base = os.environ.get("COOLAPK_PUBLIC_BASE", "").rstrip("/")
    if not public_base:
        return json.dumps(
            {
                "error": "未启用的功能：本进程没有配置 COOLAPK_PUBLIC_BASE",
                "hint": "stdio 本地模式没有对外地址，请直接用 coolapk_read_image 返回图片；"
                "远程 HTTP 部署请设置 COOLAPK_PUBLIC_BASE=https://<域名>",
            },
            ensure_ascii=False,
        )
    # 与 /img 端点同源解析签名密钥：此前这里独立读 token 并回落到硬编码
    # "coolapk-mcp-dev"，与端点侧（随机持久化密钥）不一致时签名必然对不上，
    # 且硬编码密钥等于公开签名——任何知道字符串的人都能伪造 /img 链接。
    secret = _resolve_img_secret(os.environ.get("COOLAPK_HTTP_TOKEN", "").strip())
    payload = build_image_links(urls, public_base, secret, ttl_minutes)
    return json.dumps(payload, ensure_ascii=False)


@mcp.tool()
@_catch_api_error
async def coolapk_get_url(path: str) -> str:
    """只读透传：GET 任意酷安 /v6/ 只读接口路径并返回裁剪后的 JSON。

    仅允许 api.coolapk.com 的 /v6/ 开头路径；含写动作的端点
    （点赞/回复/关注/发帖/私信等）一律 403 拒绝。响应保留原始
    结构（键名为酷安原始 camelCase），仅剔除 cookie/token 等敏感字段。
    适合获取列表页未覆盖的数据（应用详情、板块、二级评论等）。
    默认开启严格模式：额外要求末段形态像只读端点
    （detail/list/info/space 等），未知写端点会被提前挡住；
    设 COOLAPK_GET_URL_STRICT=0 可关闭（透传非常规只读路径时用）。

    Args:
        path: 接口路径或完整 URL，如 /v6/apk/detail?id=com.tencent.mm
    """
    client = await _get_client()
    data = await client.get_url(path)
    return _json(data, whitelist="get_url")


def _warn_login_state() -> None:
    """HTTP 服务启动前检查登录态，缺失时打印修复指引（不拒绝启动）。"""
    import sys

    try:
        config = AppConfig.load()
    except Exception as exc:  # 配置文件损坏等，不让启动挂掉
        print(
            f"[coolapk-mcp] 登录态读取失败（{exc}），服务照常启动但所有工具将返回 auth_expired。",
            file=sys.stderr,
        )
        return
    if not config.is_logged_in:
        print(
            "[coolapk-mcp] 未检测到有效登录态（远程服务器无酷安桌面端，auto 模式读不到 accounts.json）。\n"
            "  请在服务器上建立 native 登录态，三选一：\n"
            "    1. coolapk login --sms    # 服务器直接收验证码\n"
            "    2. coolapk login --cookie # 从手机抓包导入 Cookie\n"
            "    3. 本地 coolapk login --adb 后，把 ~/.coolapk-mcp/config.json 上传到服务器\n"
            "  并把酷安 APK 放到 ~/.coolapk-mcp/coolapk.apk（用于提取 auth_blob）。",
            file=sys.stderr,
        )


_PROCESS_STARTED = time.monotonic()


async def _status_payload(client: CoolapkClient) -> dict:
    """运行状态快照：版本、运行时长、登录态、限速参数、缓存与请求指标

    与 /health 分工：/health 只回 ok 给容器探针（不碰磁盘、不建 client），
    /status 才做完整体检，供人看与外部监控抓取。
    """
    try:
        cfg = AppConfig.load()
        login = {
            "logged_in": cfg.is_logged_in,
            "uid": cfg.uid or None,
            "mode": cfg.login_mode,
        }
    except Exception as exc:  # 配置损坏不该让状态接口 500
        logger.warning("读取登录态失败：%s", exc)
        login = {"logged_in": False, "uid": None, "mode": "unknown"}
    return {
        "status": "ok",
        "version": __version__,
        "uptime_s": round(time.monotonic() - _PROCESS_STARTED, 1),
        "login": login,
        "rate_limit": {
            "mode": throttle.RATE_LIMIT_MODE,
            "min_interval_s": throttle.MIN_INTERVAL,
            "max_retries": throttle.MAX_RETRIES,
            "max_backoff_s": throttle.MAX_BACKOFF,
        },
        "cache": client.cache_stats(),
        "requests": client.metrics(),
        "image": {"max_bytes": MAX_IMAGE_BYTES},
    }


def _build_http_app():
    """构造带 Bearer Token 鉴权的 streamable-http ASGI 应用。"""
    import ipaddress
    import socket

    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Mount, Route

    host = os.environ.get("COOLAPK_HTTP_HOST", "127.0.0.1")
    port_raw = os.environ.get("COOLAPK_HTTP_PORT", "8000")
    try:
        port = int(port_raw)
    except ValueError:
        raise SystemExit(
            f"[coolapk-mcp] COOLAPK_HTTP_PORT={port_raw!r} 不是有效端口号，"
            "请设置为 1-65535 的整数。"
        ) from None
    token = os.environ.get("COOLAPK_HTTP_TOKEN", "")
    _warn_token_weakness(token)

    # 非回环地址必须显式配 token，防止登录态裸奔公网。
    # 先按字面 IP 解析（直接支持 ::1 等 IPv6 回环，getaddrinfo 在部分平台会失败），
    # 失败再走域名解析；都解析不出按公网对待，从严。
    try:
        addr = ipaddress.ip_address(host)
        is_loopback = addr.is_loopback
    except ValueError:
        try:
            addr = ipaddress.ip_address(socket.gethostbyname(host))
            is_loopback = addr.is_loopback
        except (socket.gaierror, ValueError):
            is_loopback = False
    allow_anonymous = os.environ.get("COOLAPK_ALLOW_ANONYMOUS", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    # streamable-http 一律要求 token：只听回环也不够安全——本机浏览器里的任意网页
    # 可以借 DNS rebinding 打到 127.0.0.1（本项目显式关掉了 rebinding 保护），
    # 就等于把 17 个工具连同登录态交给它。
    if not token and not allow_anonymous:
        where = "回环地址" if is_loopback else "非回环地址"
        raise SystemExit(
            f"[coolapk-mcp] 拒绝启动：COOLAPK_HTTP_HOST 绑定{where}（{host}）"
            "但未设置 COOLAPK_HTTP_TOKEN。\n"
            "  streamable-http 传输一律要求 Bearer 鉴权；HTTP 服务不设防 = 任何人都能\n"
            "  用你的酷安登录态查数据。\n"
            "  设置 COOLAPK_HTTP_TOKEN=<随机长串> 后重试（公网部署请用 Nginx/Caddy\n"
            "  反代加 TLS，见 README 远程部署章）。仅离线本机调试确需免鉴权时，\n"
            "  显式设 COOLAPK_ALLOW_ANONYMOUS=1。"
        )

    # ---- 图片中转：把酷安 CDN 的防盗链图片转成可公开访问的签名链接 ----
    # 酷安 image.coolapk.com 对非客户端请求返回 HTTP 567，且多数聊天类客户端
    # （如小爱）拿不到 base64 就显示不了图，因此提供 /img 中转 + HMAC 签名。
    public_base = os.environ.get("COOLAPK_PUBLIC_BASE", "").rstrip("/")
    if public_base and not token:
        logger.warning(
            "COOLAPK_PUBLIC_BASE 已设置但 COOLAPK_HTTP_TOKEN 为空："
            "/img 将使用本机随机签名密钥（非硬编码），仍建议配置长 token 以便轮换"
        )
    img_secret = _resolve_img_secret(token)
    state = {"token": token, "public_base": public_base, "img_secret": img_secret}

    def _auth_ok(scope) -> bool:
        """Bearer 校验判定（MCP 端点与 /status 共用同一份逻辑）。

        未设置 token 时（显式 COOLAPK_ALLOW_ANONYMOUS=1）一律放行。
        全程按字节比较：先 decode 成 str 再 encode 会变成 UTF-8 往返，
        非 ASCII token 在标准客户端上恒定 401、在 latin-1 原始字节下反而通过。
        """
        if not state["token"]:
            return True
        raw_headers = scope.get("headers") or []
        auth_values = [v for k, v in raw_headers if k.lower() == b"authorization"]
        if len(auth_values) != 1:
            # 缺失或重复：重复头会让反代与进程各自取到不同的 Authorization
            return False
        scheme, _, cred = auth_values[0].partition(b" ")
        return scheme.lower() == b"bearer" and hmac.compare_digest(
            cred, state["token"].encode("utf-8")
        )

    def _unauthorized():
        return JSONResponse(
            {"error": "unauthorized", "detail": "缺少或错误的 Bearer Token"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )

    async def _status(_request):
        """/status：一次拿到版本、登录态、限速、缓存与请求指标（与 MCP 同鉴权）"""
        if not _auth_ok(_request.scope):
            return _unauthorized()
        try:
            client = await _get_client()
            return JSONResponse(await _status_payload(client))
        except Exception as exc:
            # 探针不该因为配置损坏或客户端初始化失败拿 500：
            # 200 + degraded 才能区分「进程活着但坏了」与「进程没了」
            return JSONResponse(
                {"status": "degraded", "version": __version__, "error": type(exc).__name__}
            )

    def _bearer_endpoint(inner_app):
        """Bearer Token 校验包裹 MCP 端点：/health 免鉴权，MCP 一律校验。"""

        async def _endpoint(scope, receive, send):
            if not _auth_ok(scope):
                await _unauthorized()(scope, receive, send)
                return
            await inner_app(scope, receive, send)

        return _endpoint

    def _health(request):
        # 不暴露任何状态细节，只证明服务活着
        return JSONResponse({"status": "ok"})

    # 图片中转并发闸门（见上方 _img 内注释）
    img_semaphore = asyncio.Semaphore(4)

    def _img_sig(url: str, exp: int) -> str:
        return hmac.new(
            state["img_secret"].encode(), f"{url}|{exp}".encode(), hashlib.sha256
        ).hexdigest()[:32]

    async def _img(request):
        """图片中转：校验签名与域名白名单后，带客户端特征去酷安 CDN 取图。"""
        url = request.query_params.get("u") or ""
        exp_raw = request.query_params.get("exp") or ""
        sig = request.query_params.get("sig") or ""
        if not url or not exp_raw or not sig:
            return JSONResponse(
                {"error": "bad_request", "detail": "缺少 u / exp / sig 参数"}, status_code=400
            )
        try:
            exp = int(exp_raw)
        except ValueError:
            return JSONResponse(
                {"error": "bad_request", "detail": "exp 必须是整数时间戳"}, status_code=400
            )
        if exp < int(time.time()):
            return JSONResponse(
                {"error": "expired", "detail": "链接已过期，请重新生成"}, status_code=403
            )
        # 先做格式闸门再比较：hmac.compare_digest 对含非 ASCII 的 str 会抛
        # TypeError（免鉴权端点可被任意扫描器稳定打成 500），且长度不等时
        # compare_digest 仍会做完整比较，提前按十六进制形态挡掉更省事
        if len(sig) != 32 or any(c not in "0123456789abcdef" for c in sig):
            return JSONResponse(
                {"error": "bad_signature", "detail": "签名校验失败"}, status_code=403
            )
        # 先验签名、再验 host：反过来的话任何人不需要有效签名就能通过
        # bad_signature/forbidden_host 的差异判断某域名是否在白名单里
        if not hmac.compare_digest(sig.encode(), _img_sig(url, exp).encode()):
            return JSONResponse(
                {"error": "bad_signature", "detail": "签名校验失败"}, status_code=403
            )
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or (parsed.hostname or "").lower() not in IMAGE_HOSTS:
            return JSONResponse(
                {"error": "forbidden_host", "detail": "只允许转发酷安图片域名"}, status_code=403
            )
        if parsed.port not in (None, 80, 443):
            # 端口不限制的话，签名持有者可以让服务端对酷安主机任意端口建连
            return JSONResponse(
                {"error": "forbidden_host", "detail": "只允许 80/443 端口"}, status_code=403
            )
        # 缩略优先：带 @WxH 尺寸后缀的图先试酷安服务端缩略 .m.jpg（大图省约 40% 流量），
        # 上游非 200 自动回落原图；COOLAPK_IMG_THUMBNAIL=0 关闭。
        want_thumb = os.environ.get("COOLAPK_IMG_THUMBNAIL", "1").lower() not in ("0", "false")
        thumb_url = _thumb_variant(url) if want_thumb else None
        # HEAD 只做探活：不读上游正文，也不把图片塞进响应体
        is_head = request.method == "HEAD"
        fetch_method = "HEAD" if is_head else "GET"
        fetch_headers = {
            "User-Agent": IMAGE_UA,
            "Referer": "https://www.coolapk.com/",
        }
        try:
            # 共享连接池：此前每张图都新建 AsyncClient，图片墙场景下 TLS 握手是
            # 主要开销；这里复用进程级 client（关停时由 lifespan 统一关闭）
            client = image_client()
            # 免鉴权端点 + 单请求最多缓冲 15MB：不设并发闸门时 20 个并发就能
            # 占掉 300MB 常驻内存
            async with img_semaphore:
                status, body, ctype, err, err_status = await _fetch_upstream_image(
                    client, thumb_url or url, fetch_headers, method=fetch_method
                )
                if thumb_url and status != 200 and err is None:
                    # 缩略不存在（404 等）：回落原图
                    status, body, ctype, err, err_status = await _fetch_upstream_image(
                        client, url, fetch_headers, method=fetch_method
                    )
        except Exception as exc:  # 网络异常统一 502，不泄漏细节
            return JSONResponse(
                {"error": "fetch_failed", "detail": type(exc).__name__}, status_code=502
            )
        if err is not None:
            # 跳转越界 403 / 图片过大 413：直接返回结构化错误
            return JSONResponse(err, status_code=err_status)
        if status != 200:
            return JSONResponse(
                {"error": "upstream_status", "detail": f"上游返回 {status}"},
                status_code=502,
            )
        # 只放行四种位图：image/svg+xml 是活文档，同源嵌入时可执行脚本
        base_ctype = ctype.split(";", 1)[0].strip().lower()
        if base_ctype not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
            base_ctype = "image/jpeg"
        # 缓存寿命跟随签名：固定 86400 会让过期链接被中间层继续供给一天
        max_age = max(0, exp - int(time.time()))
        return Response(
            b"" if is_head else body,
            media_type=base_ctype,
            headers={
                "Cache-Control": f"public, max-age={max_age}",
                "X-Content-Type-Options": "nosniff",
            },
        )

    http_app = mcp.streamable_http_app()

    import contextlib

    @contextlib.asynccontextmanager
    async def _lifespan(app):
        # 等价于 streamable_http_app() 内置 lifespan：驱动 session_manager.run()
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            # HTTP 服务退出/容器重启时释放全局 httpx 连接池，
            # 避免 keep-alive 连接与后台任务悬挂到进程强制结束
            await _reset_client()
            await _close_retired()
            await aclose_image_client()

    routes = [
        Route("/health", _health, methods=["GET"]),
        Route("/status", _status, methods=["GET"]),
        Route("/img", _img, methods=["GET", "HEAD"]),
        Mount(
            "/",
            app=_bearer_endpoint(http_app),
        ),
    ]
    # Bearer 校验包住整个 streamable-http 子应用（原生 /mcp 端点原样可达，
    # 含 GET/POST/DELETE 与 GET /mcp 建流），/health 在子应用外免鉴权。
    return Starlette(routes=routes, lifespan=_lifespan), host, port


def main():
    setup_logging()
    transport = os.environ.get("COOLAPK_MCP_TRANSPORT", "stdio").strip().lower()
    if transport in ("http", "streamable-http", "streamable_http"):
        _warn_login_state()
        app, host, port = _build_http_app()
        import uvicorn

        # lifespan 驱动 session_manager.run()，缺了会报
        # "Task group is not initialized. Make sure to use run()."
        config = uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="on")
        server = uvicorn.Server(config)
        server.run()
    elif transport == "stdio":
        mcp.run()
    else:
        raise SystemExit(
            f"[coolapk-mcp] 未知 COOLAPK_MCP_TRANSPORT={transport!r}，"
            "可选 stdio（默认）或 streamable-http。"
        )


if __name__ == "__main__":
    main()
