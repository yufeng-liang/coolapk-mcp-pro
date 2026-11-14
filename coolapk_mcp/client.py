"""HTTP 客户端 + API 封装 — 合并网络层和业务端点"""

import asyncio
import email.utils
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from coolapk_mcp import throttle
from coolapk_mcp.auth.libauth import APK_CACHE_FILE
from coolapk_mcp.auth.token_v3 import DEFAULT_APP_VERSION, DEFAULT_VERSION_CODE, generate_token_v3
from coolapk_mcp.budget import Budget, BudgetExceeded, FetchResult
from coolapk_mcp.config import AppConfig, has_valid_sessid

logger = logging.getLogger(__name__)


# 登录态失效统一提示（error_type=auth_expired，AI 客户端据此引导用户重登）
AUTH_EXPIRED_MESSAGE = (
    "登录态已失效：若已安装酷安第三方桌面端（github.com/coolapk-desktop/coolapk-desktop），"
    "在桌面端重新登录后直接重试即可（本服务 auto 模式会自动复用其登录态，无需重启）；"
    "或运行 coolapk login --web 重新导入登录 Cookie（也可用 login --sms 短信登录、"
    "login --cookie 手动导入）；若按上述方式重新登录后仍提示失效，请重启 MCP 会话"
)


# ---- 图片下载：进程级共享连接池 ----
# 此前每张图都新建 httpx.AsyncClient（一次 TLS 握手 + 连接池即用即弃），连续
# 读图（详情页常见 9 张配图）时大部分时间花在建连上。改为共享 client，
# 只把「带 Referer 的单次请求」交给它。
IMAGE_MAX_BYTES = 15 * 1024 * 1024
_IMAGE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_IMAGE_CLIENTS: dict[bool, "httpx.AsyncClient"] = {}
# 测试注入点：非 None 时直接复用（测试用 MockTransport 建一个即可）
_IMAGE_CLIENT_OVERRIDE: "httpx.AsyncClient | None" = None


def image_client(direct: bool = False) -> "httpx.AsyncClient":
    """取进程级共享图片 client；direct=True 表示忽略环境代理（兜底路径）。"""
    if _IMAGE_CLIENT_OVERRIDE is not None:
        return _IMAGE_CLIENT_OVERRIDE
    client = _IMAGE_CLIENTS.get(direct)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            trust_env=not direct,
            headers={"User-Agent": _IMAGE_UA},
            limits=httpx.Limits(
                max_connections=16, max_keepalive_connections=8, keepalive_expiry=30.0
            ),
        )
        _IMAGE_CLIENTS[direct] = client
    return client


async def aclose_image_client() -> None:
    """关闭共享图片 client（进程退出 / 服务关停时调用）。"""
    clients = list(_IMAGE_CLIENTS.values())
    _IMAGE_CLIENTS.clear()
    for client in clients:
        if not client.is_closed:
            await client.aclose()


# 酷安图片 CDN 白名单（download_image 与 /img 中转共用同一份）
_IMAGE_HOSTS = frozenset({"image.coolapk.com", "avatar.coolapk.com", "static.coolapk.com"})
_IMAGE_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


def _image_url_ok(candidate: str) -> bool:
    """图片 URL 是否指向白名单域名（不含 userinfo 混淆与非常规端口）。"""
    parts = urlsplit(candidate)
    if parts.scheme not in ("http", "https"):
        return False
    if (parts.hostname or "").lower() not in _IMAGE_HOSTS:
        return False
    return parts.port in (None, 80, 443)


async def _fetch_image_bytes(
    target: str, headers: dict[str, str]
) -> tuple[int, bytes, str]:
    """用共享 client 拉取图片字节，带 15MB 上限与环境代理兜底。

    返回 (状态码, 字节, content-type)；非 200 时字节为空。
    超过上限抛 CoolapkError(413)：宁可明确报「图太大」，也不把进程内存撑爆。
    """
    for direct in (False, True):
        client = image_client(direct=direct)
        try:
            current = target
            # 手工跟跳转：每一跳发请求前先验域名。交给 httpx 自动跟随就是
            # 「先连上内网地址、再看结果」——连接已经发出去，等于可读 SSRF。
            for _hop in range(4):
                if not _image_url_ok(current):
                    raise CoolapkError(
                        f"图片跳转到非白名单域名或端口: {urlsplit(current).netloc or '?'}",
                        error_code=403, kind="http",
                    )
                async with client.stream(
                    "GET", current, headers=headers, follow_redirects=False
                ) as resp:
                    ctype = resp.headers.get("content-type") or ""
                    if resp.status_code in _IMAGE_REDIRECT_CODES:
                        location = resp.headers.get("location") or ""
                        if not location:
                            return resp.status_code, b"", ctype
                        current = str(httpx.URL(current).join(location))
                        continue
                    return await _read_image_response(resp, ctype)
            raise CoolapkError(
                "图片下载失败: 上游跳转超过 3 次", error_code=403, kind="http"
            )
        except httpx.InvalidURL:
            # NO_PROXY 等环境变量含非法值（如 [::1]）时 httpx 解析失败：换直连 client 重试
            if direct:
                raise
            continue
        except httpx.HTTPError as exc:
            raise CoolapkError(f"图片下载失败: {exc}", kind="network") from exc
    return 0, b"", ""


async def _read_image_response(resp: "httpx.Response", ctype: str) -> tuple[int, bytes, str]:
    """读取图片响应体：声明长度与流式累计两道 15MB 闸门。"""
    if resp.status_code != 200:
        return resp.status_code, b"", ctype
    limit_mb = IMAGE_MAX_BYTES // (1024 * 1024)
    declared = (resp.headers.get("content-length") or "").strip()
    if declared.isdigit() and int(declared) > IMAGE_MAX_BYTES:
        raise CoolapkError(
            f"图片超过 {limit_mb}MB 上限，已拒绝下载", error_code=413, kind="http"
        )
    buf = bytearray()
    async for chunk in resp.aiter_bytes():
        buf.extend(chunk)
        if len(buf) > IMAGE_MAX_BYTES:
            raise CoolapkError(
                f"图片超过 {limit_mb}MB 上限，已拒绝下载", error_code=413, kind="http"
            )
    return 200, bytes(buf), ctype


class CoolapkError(Exception):
    """API 业务错误

    kind: api(服务端业务错误) captcha(风控验证码) network(超时/连接)
          transient(5xx/429等可重试) http(其他HTTP错误) parse(响应解析失败)
          auth_expired(登录态失效) config(本地配置/资源缺失，如缺 libauth blob)

    retry_after: 仅 captcha / risk / rate_limit 类有实际意义——建议等待秒数，
                 None 表示未知/不适用
    retryable: 客户端是否可以「原样重试」。network/transient/rate_limit 为 True，
               http/auth_expired/api/captcha/risk/parse/config 为 False——上层据此
               决定是提示用户稍后重试，还是引导重登/换设备。
    """

    def __init__(
        self,
        message: str,
        error_code: int = -1,
        kind: str = "api",
        retry_after: float | None = None,
        retryable: bool = False,
    ):
        self.message = message
        self.error_code = error_code
        self.kind = kind
        self.retry_after = retry_after
        self.retryable = retryable
        super().__init__(message)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_flag(name: str, default: bool = False) -> bool:
    """布尔开关：1/true/yes/on 为真，0/false/no/off 为假，其余回默认"""
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


# 403 响应体里出现这些词，说明是风控/频率类拦截而非登录失效。
# 这类情况把 kind 归成 risk：重置登录态（清 client）解决不了问题，
# 反倒会让上层误判成「需要用户重新登录」。
_RISK_HINTS = (
    "频繁", "风控", "验证", "稍后", "限制", "拦截", "禁止",
    "captcha", "forbidden", "rate limit", "too many", "blocked",
)


def _retry_after_seconds(resp: "httpx.Response") -> float | None:
    """解析 Retry-After 头（秒数或 HTTP-date），拿不到返回 None"""
    raw = (resp.headers.get("retry-after") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


# 只读端点的「形态特征」：末段以这些词收尾的一律视为读端点
# （detail/list/info/space 等）。严格模式下用它做正向白名单，
# 反向命中写动词的一律拒绝，宁可让用户显式关掉严格模式。
_READ_SUFFIXES = (
    "list", "detail", "index", "info", "space", "profile", "explore",
    "recommend", "suggest", "search", "hot", "latest", "refresh", "init",
    "share", "count", "meta", "timeline", "history", "rank", "feeds",
    "comments", "replys", "board", "boards", "tag", "tags", "status",
    "checklogininfo", "requestvalidate", "notification", "notice", "feed",
)


# 少数只读端点的路径就是「模块名」本身（/v6/search、/v6/main/init 的近亲），
# 没有子段可判形态，单独放行
_READ_MODULES = ("search", "main", "board", "notification", "message-like")


def _looks_read_only(path_part: str) -> bool:
    """按末段形态判断是否是只读端点（严格模式的正向白名单）

    只看末段：/v6/feed/detail 的 detail 命中，/v6/feed/like 的 like 不命中；
    /v6/user/123 这类以纯数字结尾的也放行（酷安没有数字结尾的写端点）。
    """
    segs = [seg.lower() for seg in path_part.strip("/").split("/") if seg]
    if len(segs) < 2 or segs[0] != "v6":
        return False
    tail = segs[1:]
    if len(tail) == 1:
        return tail[0] in _READ_MODULES
    last = tail[-1]
    if last.isdigit():
        return True
    return any(last.endswith(suffix) for suffix in _READ_SUFFIXES)


def _response_hint(resp: "httpx.Response", limit: int = 200) -> str:
    """从错误响应里抠一句可读的话：JSON 取 message/error/msg，纯文本原样截断

    HTML 错误页（网关、CDN）没有信息量，返回空串，避免把整页标签塞进错误文案。
    """
    try:
        text = (resp.text or "").strip()
    except Exception:  # 解码失败（编码损坏 / 已被消费）都不该影响错误分类
        return ""
    if not text:
        return ""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return "" if text.startswith("<") else text[:limit]
    if isinstance(data, dict):
        for key in ("message", "error", "msg"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()[:limit]
    return ""


def _extract_request_hash(html: str) -> str:
    """从登录页 HTML 提取 requestHash，兼容新老两种页面格式。

    - 老格式：requestHash: 'xxxx'
    - 新格式（2026-11 页面改版）：
      var authFlow = createAuthCaptchaFlow('/auth/login?type=mobile', 'xxxx')
      只匹配调用处（两个参数均为引号字符串），函数定义行
      function createAuthCaptchaFlow(url, requestHash) 不会误命中。
    """
    m = re.search(r"requestHash[:\s]+['\"]([^'\"]+)['\"]", html)
    if m:
        return m.group(1)
    m = re.search(
        r"createAuthCaptchaFlow\s*\(\s*['\"][^'\"]+['\"]\s*,\s*['\"]([^'\"]+)['\"]", html
    )
    if m:
        return m.group(1)
    return ""


def _consume_future_exception(fut: asyncio.Future) -> None:
    """主动消费 Future 的异常。

    单飞 Future 的异常由等待者 await 时取回；但等待者可能全部提前取消，
    没人检索异常时 asyncio 会打印 "Future exception was never retrieved" 告警，
    这里在 done 回调里取一次把它吃掉（已取消的 Future 不取）。
    """
    if fut.cancelled():
        return
    fut.exception()


def _env_int(name: str, default: int) -> int:
    """读整数环境变量；空值或非法值静默回退默认值。"""
    try:
        return int(float(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


class CoolapkClient:
    """酷安 API 客户端"""

    BASE_URL = "https://api.coolapk.com"

    # 端点被验证码拦截后的冷却期（秒）：期间不再撞该端点，直接走回落
    CAPTCHA_COOLDOWN = 600.0
    # 解验证码的入口端点必须豁免冷却机制，否则被风控后无法自助解锁（会死锁一个冷却期）
    CAPTCHA_UNLOCK_PATHS = ("/v6/account/requestValidate",)
    # 详情回落时最多翻作者动态的页数
    FALLBACK_SCAN_PAGES = 3
    # 一页的基准条数：末页判定与可翻页上限都由它推导。酷安接口哪天换页大小，
    # 只改 COOLAPK_PAGE_SIZE 即可，不用满仓库找魔数 10
    PAGE_SIZE = _env_int("COOLAPK_PAGE_SIZE", 10)
    # feed_id → 作者uid 索引，持久化到 ~/.coolapk-mcp/uid_index.json
    INDEX_FILE = Path.home() / ".coolapk-mcp" / "uid_index.json"
    INDEX_MAX = 2000
    # X-App-Token 短 TTL 复用窗口（秒）：0 表示每次请求都重算（排障用）
    TOKEN_CACHE_TTL = _env_float("COOLAPK_TOKEN_TTL", 60.0)
    # uid 索引落盘的合并窗口（秒）：窗口内的新 id 累积成一次写
    INDEX_SAVE_DELAY = _env_float("COOLAPK_INDEX_SAVE_DELAY", 1.0)
    # get_url 严格模式：默认关（保留「未覆盖只读接口」的逃生舱），
    # 打开后除黑名单外还要求末段形态像只读端点，未知新写端点也会被挡住
    GET_URL_STRICT = _env_flag("COOLAPK_GET_URL_STRICT", True)

    def __init__(self, config: AppConfig | None = None):
        self.config = config or AppConfig.load()
        try:
            self._client = httpx.AsyncClient(timeout=30.0)
        except httpx.InvalidURL:
            # NO_PROXY 等环境变量含非法值（如 [::1]）时 httpx 会解析失败，禁用环境代理兜底
            self._client = httpx.AsyncClient(timeout=30.0, trust_env=False)
        self._limiter = throttle.RateLimiter()
        self._cache = throttle.TTLCache()
        # 单飞表：cache_key -> 在途请求的 Future，同 key 的并发 GET 共用一次网络请求
        self._inflight: dict[str, asyncio.Future] = {}
        self._blocked: dict[str, float] = {}
        self._uid_index: dict[int, int] = {}
        self._token_value: str = ""
        self._token_at: float = 0.0
        # token 单飞：TTL 到期瞬间的并发请求共用一次 bcrypt 计算
        self._token_fut: asyncio.Future | None = None
        self._index_dirty = False
        self._index_flush_task: asyncio.Task | None = None
        # 匿名只读：COOLAPK_ALLOW_ANONYMOUS=1 且未登录时，不再本地预检拦截、
        # 也不发 X-App-Token / Cookie（token 需要登录态 blob，匿名场景拿不到）
        self.allow_anonymous = os.environ.get("COOLAPK_ALLOW_ANONYMOUS", "").strip().lower() in (
            "1", "true", "yes", "on",
        )
        # 运行指标：只统计不落盘，供 streamable-http 的 /status 观测
        self._started_at = time.monotonic()
        self._metrics: dict[str, Any] = {
            "calls": 0, "requests": 0, "retries": 0, "cache_hits": 0,
            "cache_misses": 0, "shared": 0, "errors": {},
            # 延迟按「逻辑调用」统计（含缓存命中与搭车），单位毫秒
            "latency_ms_total": 0.0, "latency_ms_max": 0.0,
        }
        self._load_uid_index()

    # ---- feed_id → uid 索引（详情回落的关键） ----

    def _load_uid_index(self) -> None:
        try:
            data = json.loads(self.INDEX_FILE.read_text(encoding="utf-8"))
            # 文件被写坏时可能是 null/list/str，dict 预检防止 AttributeError 崩穿构造
            if not isinstance(data, dict):
                self._uid_index = {}
                return
            self._uid_index = {int(k): int(v) for k, v in data.items()}
        except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
            self._uid_index = {}

    def _save_uid_index(self, snapshot: dict[int, int] | None = None) -> None:
        """落盘 uid 索引。

        snapshot 由调用方在写盘前拍好：写盘在 to_thread 里跑，直接读
        self._uid_index 会与事件循环里的更新交错，落盘内容既非旧表也非新表。
        """
        try:
            self.INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
            source = self._uid_index if snapshot is None else snapshot
            trimmed = dict(list(source.items())[-self.INDEX_MAX:])
            # 原子写：先落临时文件再 replace，防止中断产生半截 JSON 被 load 静默清空
            tmp = self.INDEX_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(trimmed), encoding="utf-8")
            os.replace(tmp, self.INDEX_FILE)
        except OSError:
            # 索引丢了只会让详情回落多扫几页，但静默失败会让「越用越慢」无从排查
            logger.warning("uid 索引写盘失败（不影响功能，下次有新 id 时重试）", exc_info=True)

    def _index_feeds(self, data: Any) -> None:
        """从列表响应记录 feed_id → 作者uid，供详情被风控时回落"""
        if not isinstance(data, list):
            return
        changed = False
        for item in data:
            if not isinstance(item, dict):
                continue
            if item.get("entityType") not in ("feed", None):
                continue
            fid = item.get("id")
            uid = item.get("uid") or (item.get("userInfo") or {}).get("uid")
            if not fid or not uid:
                continue
            try:
                self._uid_index[int(fid)] = int(uid)
                changed = True
            except (TypeError, ValueError):
                continue
        if changed:
            self._mark_index_dirty()

    def _mark_index_dirty(self) -> None:
        """标记索引脏并安排一次延迟落盘，避免在事件循环里同步写盘

        原先每来一个新 feed_id 就把整份索引（最多 2000 条）写一次盘，
        高频翻页时等于每页一次磁盘 I/O 且全程占住事件循环。改为 1s 合并窗口，
        写盘本身再用 to_thread 卸载；进程退出前由 close() 兜底落盘。
        """
        self._index_dirty = True
        if self._index_flush_task is not None and not self._index_flush_task.done():
            return
        try:
            self._index_flush_task = asyncio.get_running_loop().create_task(
                self._flush_index_later()
            )
        except RuntimeError:
            # 没有运行中的事件循环（同步调试路径）：退回立即写，保证不丢数据
            self._index_dirty = False
            self._save_uid_index()

    async def _flush_index_later(self) -> None:
        try:
            await asyncio.sleep(self.INDEX_SAVE_DELAY)
        except asyncio.CancelledError:
            raise
        if not self._index_dirty:
            return
        # 先拍快照、再清 dirty：写盘期间新到的 id 会把 dirty 再置 True，
        # 下一轮补写；反过来（先清后拍）会让这期间的更新永久丢在内存里
        snapshot = dict(self._uid_index)
        self._index_dirty = False
        try:
            await asyncio.to_thread(self._save_uid_index, snapshot)
        except OSError:
            logger.warning("uid 索引后台写盘失败", exc_info=True)

    async def _app_token(self) -> str:
        """取 X-App-Token：短 TTL 复用 + 线程卸载

        一次 v3 生成是 bcrypt cost=10 的纯 CPU 运算（约 60ms），原先每个请求
        都在事件循环里同步算一遍，既浪费算力又把并发全堵死。token 只校验
        设备/版本一致性，短窗口内复用完全安全（默认 60s，可关）。
        """
        ttl = self.TOKEN_CACHE_TTL
        now = time.monotonic()
        if ttl > 0 and self._token_value and now - self._token_at < ttl:
            return self._token_value

        # token 单飞：TTL 到期瞬间多个并发请求会同时发现缓存失效，
        # 不合并的话每个都算一遍 bcrypt（约 60ms 纯 CPU），把线程池占满
        inflight = self._token_fut
        if inflight is not None and not inflight.done():
            return await asyncio.shield(inflight)

        inflight = asyncio.get_running_loop().create_future()
        inflight.add_done_callback(_consume_future_exception)
        self._token_fut = inflight
        try:
            token, _ = await asyncio.to_thread(generate_token_v3, self.config.device_code)
        except BaseException as exc:
            err: CoolapkError | BaseException
            if isinstance(exc, asyncio.CancelledError):
                err = exc
            else:
                # 这里捕获面要宽：blob 缺失可能抛 RuntimeError/OSError，
                # 解析异常可能是 ValueError/KeyError，都该归成 config 并给出修复路径
                err = CoolapkError(
                    f"无法生成 X-App-Token（{type(exc).__name__}: {exc}）"
                    f"修复：运行 coolapk login 重新导入登录态，"
                    f"或把酷安 APK 放到 {APK_CACHE_FILE}（下次生成 token 时自动提取 blob）",
                    kind="config",
                )
            if not inflight.done():
                if isinstance(err, asyncio.CancelledError):
                    inflight.cancel()
                else:
                    inflight.set_exception(err)
            if isinstance(err, asyncio.CancelledError):
                raise
            raise err from exc
        finally:
            self._token_fut = None
        if ttl > 0:
            self._token_value = token
            self._token_at = time.monotonic()
        if not inflight.done():
            inflight.set_result(token)
        return token

    async def _build_headers(self) -> dict[str, str]:
        """组装请求头（X-App-Token 由 _app_token 缓存与线程卸载）"""
        headers = {
            "X-Sdk-Int": "35",
            "X-Sdk-Locale": "zh-CN",
            "X-App-Mode": "universal",
            "X-App-Channel": "coolapk",
            "X-App-Id": "com.coolapk.market",
            "X-App-Device": self.config.device_code,
            "X-App-Version": DEFAULT_APP_VERSION,
            "X-App-Code": str(DEFAULT_VERSION_CODE),
            "X-Api-Version": "16",
            "X-App-Supported": str(DEFAULT_VERSION_CODE),
            "X-Dark-Mode": "0",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": (
                "Dalvik/2.1.0 (Linux; U; Android 16; 23113RKC6C Build/AQ3A.250226.002) "
                "(#Build; Redmi; 23113RKC6C; AQ3A.250226.002; HyperOS_3.0; 3.0.1.0) "
                f"+CoolMarket/{DEFAULT_APP_VERSION}-{DEFAULT_VERSION_CODE}-universal"
            ),
        }
        if not (self.allow_anonymous and not self.config.is_logged_in):
            # 匿名只读时不带 X-App-Token：生成它需要登录态 auth blob，匿名场景本来就没有
            headers["X-App-Token"] = await self._app_token()
        return headers

    def _get_cookie_header(self) -> str | None:
        """返回 Cookie 字符串。直接拼字符串避免 httpx cookies 参数对中文 username 的编码问题。

        桌面端导入的完整 Cookie 串（含 SESSID 等）直接整串使用；
        否则按 APP 端三键（uid/username/token）拼接。
        """
        if not self.config.is_logged_in:
            return None
        if self.config.desktop_cookie and has_valid_sessid(self.config.desktop_cookie):
            return self.config.desktop_cookie
        return f"uid={self.config.uid}; username={quote(self.config.username, safe='')}; token={self.config.token}"

    def _check_local_auth(self) -> None:
        """登录前本地校验：未登录或完整 Cookie 串 SESSID 已失效直接抛 auth_expired，不发探测请求

        桌面端导出串形如 "; SESSID=..."（分号后可能带空格，config.py 同款注释），
        判定统一交给 has_valid_sessid（按键解析、strip 处理），不用子串匹配，
        避免漏检 "; SESSID=deleted" 这类带空格变体；SMS 登录只落三键串（无
        SESSID），不算失效。
        """
        if not self.config.is_logged_in:
            # 匿名只读模式下未登录不算错误：酷安的只读接口多数允许匿名访问
            if getattr(self, "allow_anonymous", False):
                return
            raise CoolapkError(AUTH_EXPIRED_MESSAGE, kind="auth_expired")
        cookie = self.config.desktop_cookie
        if cookie and not has_valid_sessid(cookie) and "sessid=" in cookie.lower():
            raise CoolapkError(AUTH_EXPIRED_MESSAGE, kind="auth_expired")

    def _cache_key(self, path: str, params: dict | None = None) -> str:
        """缓存/单飞键：身份（uid + token 短哈希）+ 路径 + 排序后的参数。

        只带 path+params 时，同一进程内切换登录态（desktop 多账号 / auto 模式
        回落本机 config）会把 A 账号的响应喂给 B 账号；token 为空（未登录）也
        取空串哈希，保证键始终稳定。
        """
        token8 = hashlib.sha256((self.config.token or "").encode("utf-8")).hexdigest()[:8]
        # device_code 也进键：它决定 X-App-Token 的生成输入，换设备码后同一个
        # path+params 拿到的其实是另一套身份下的响应，不能再共用缓存
        def _norm(value: Any) -> str:
            # 对齐 httpx 的查询串编码：None → 空串，bool → true/false，
            # 否则 str(v) 会把两者拼成 "None"/"True"，与真正发出的请求不同键
            if value is None:
                return ""
            if isinstance(value, bool):
                return "true" if value else "false"
            return str(value)

        norm = "&".join(
            f"{key}={_norm(value)}"
            for key, value in sorted((params or {}).items(), key=lambda kv: str(kv[0]))
        )
        return f"{self.config.uid}:{token8}:{self.config.device_code}:{path}?{norm}"

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        """统一入口：按错误 kind 与耗时记账后原样抛出（指标只给 /status 看，不影响语义）"""
        started = time.monotonic()
        self._metrics["calls"] += 1
        try:
            return await self._request_inner(method, path, **kwargs)
        except CoolapkError as exc:
            errors = self._metrics["errors"]
            errors[exc.kind] = errors.get(exc.kind, 0) + 1
            raise
        finally:
            elapsed_ms = (time.monotonic() - started) * 1000
            self._metrics["latency_ms_total"] += elapsed_ms
            if elapsed_ms > self._metrics["latency_ms_max"]:
                self._metrics["latency_ms_max"] = elapsed_ms

    async def _request_inner(self, method: str, path: str, **kwargs) -> Any:
        """统一请求方法：本地登录预检 → GET 缓存 → 单飞合并 → 限速/重试 → 错误分类

        单飞（single-flight）只覆盖 cache_key 非 None 的 GET：同 key 已有在途
        请求时，后到的协程直接搭车共用结果，不再多消耗一次限速配额。
        """
        # 本地预检：未登录或 SESSID 已失效直接抛 auth_expired，不发探测请求；
        # 请求类端点（登录流程自身）不受此限制
        if not path.startswith("/v6/auth/"):
            self._check_local_auth()

        # 验证码负缓存前置闸门：该端点近期已被风控拦过，冷却期内直接跳过，
        # 不再白白消耗一次限速配额、也不再往风控上撞。base 用 urlsplit 取纯路径，
        # 与下方「验证码」分支写入时同一口径。
        base = urlsplit(path).path
        if base not in self.CAPTCHA_UNLOCK_PATHS and self._endpoint_blocked(base):
            remaining = max(0.0, self._blocked.get(base, 0.0) - time.monotonic())
            raise CoolapkError(
                f"端点 {base} 处于验证码冷却期（约 {remaining:.0f}s 后解除），本次请求未发出",
                kind="captcha",
                retry_after=remaining,
            )

        if method != "GET":
            # 非 GET 没有可共享的键：直接走请求体
            return await self._request_uncached(method, path, None, **kwargs)

        params = kwargs.get("params") or {}
        cache_key = self._cache_key(path, params)
        if throttle.CACHE_ENABLED:
            cached = self._cache.get(cache_key)
            if cached is not None:
                self._metrics["cache_hits"] += 1
                return cached
            self._metrics["cache_misses"] += 1

        # ---- 单飞：同 key 已有在途请求时搭车，不再单独发一次 ----
        # 注意单飞不以 CACHE_ENABLED 为条件：关缓存只该关掉读写，不该让并发合并失效
        inflight = self._inflight.get(cache_key)
        if inflight is not None:
            # shield：等待方被取消时不连带取消真正在发请求的那一路
            self._metrics["shared"] += 1
            return await asyncio.shield(inflight)

        inflight = asyncio.get_running_loop().create_future()
        self._inflight[cache_key] = inflight
        # done 回调里主动取一次异常：等待者全部提前取消时没人检索，
        # asyncio 会打印 "Future exception was never retrieved" 告警
        inflight.add_done_callback(_consume_future_exception)
        try:
            result = await self._request_uncached(
                method, path, cache_key if throttle.CACHE_ENABLED else None, **kwargs
            )
        except asyncio.CancelledError:
            # 生产方被取消：不能直接 cancel Future——等待者会收到 CancelledError，
            # 而 asyncio 的取消是穿透性的，客户端重试逻辑根本不接这个异常。
            # 改成可重试的 network 错误，等待方拿到后能自己再发一次。
            if not inflight.done():
                inflight.set_exception(
                    CoolapkError("共享请求被取消，请重试", kind="network", retryable=True)
                )
            raise
        except BaseException as exc:
            # 其余异常（含 CoolapkError 的 kind 分类）原样转交给所有等待者
            if not inflight.done():
                inflight.set_exception(exc)
            raise
        else:
            inflight.set_result(result)
            return result
        finally:
            # 先让等待者拿到结果再出表：后到的调用即便撞上已完成 Future 也是直接返回
            self._inflight.pop(cache_key, None)

    async def _request_uncached(
        self, method: str, path: str, cache_key: str | None, **kwargs
    ) -> Any:
        """真正的请求体：限速 → 瞬态错误退避重试 → 解析 → 写缓存（单飞由 _request 编排）"""
        url = f"{self.config.api_base}{path}"
        # 验证码负缓存的键：与 _request 的前置闸门同一口径（urlsplit 取纯路径）
        base = urlsplit(path).path
        resp = None
        last_err: CoolapkError | None = None
        last_status: int | None = None
        for attempt in range(throttle.MAX_RETRIES + 1):
            self._metrics["requests"] += 1
            if attempt:
                self._metrics["retries"] += 1
            # 限速桶：默认 global 模式下参数被忽略（行为与历史版本一致）；
            # COOLAPK_RATE_LIMIT_MODE=bucket 时按资源段各自计时
            await self._limiter.acquire(throttle.bucket_for(base))
            headers = await self._build_headers()
            cookie = self._get_cookie_header()
            if cookie:
                headers["Cookie"] = cookie
            err: CoolapkError | None = None
            try:
                resp = await self._client.request(
                    method, url, headers=headers, **kwargs
                )
                resp.raise_for_status()
                last_err = None  # 本轮成功：清掉早前失败轮标记，避免循环后被误判为重试耗尽
                break
            except RuntimeError:
                # 热加载/登录重置会把旧 client 关掉，而正在用旧实例的协程会撞上
                # httpx 的 RuntimeError（不属于 httpx.HTTPError）。归成可重试 network，
                # 让调用方重来一次即可拿到新实例，而不是看到「内部错误」
                if getattr(self._client, "is_closed", False):
                    last_status = None
                    err = CoolapkError(
                        "客户端已重建（登录态变更），请重试", kind="network", retryable=True
                    )
                else:
                    raise
            except httpx.TimeoutException:
                last_status = None
                err = CoolapkError("请求超时", kind="network", retryable=True)
            except httpx.ConnectError:
                last_status = None
                err = CoolapkError("无法连接服务器", kind="network", retryable=True)
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                last_status = code
                retry_after = _retry_after_seconds(e.response)
                if code == 403:
                    hint = _response_hint(e.response)
                    if hint and any(w in hint.lower() for w in _RISK_HINTS):
                        # 风控/频率类 403：登录态可能完好，清 client 重登解决不了问题，
                        # 归成 risk 让上层提示「稍后重试」，别把用户赶去重新登录
                        raise CoolapkError(
                            f"请求被风控拦截（{hint}）",
                            error_code=403,
                            kind="risk",
                            retry_after=retry_after if retry_after is not None else 120.0,
                        ) from e
                    # 其余 403 仍是登录态失效（captchaRequired / 会话无效）
                    raise CoolapkError(
                        AUTH_EXPIRED_MESSAGE, error_code=403, kind="auth_expired"
                    ) from e
                if code == 429:
                    # 服务端明确限流：退避上限都追不上 Retry-After 时，重试只会
                    # 加深封禁，直接把这轮交回调用方（retryable=True 但要求久等）
                    err = CoolapkError(
                        "请求过于频繁（服务端限流）",
                        error_code=429,
                        kind="rate_limit",
                        retry_after=retry_after,
                        retryable=True,
                    )
                    if retry_after is not None and retry_after > throttle.MAX_BACKOFF:
                        raise err from e
                else:
                    kind = "transient" if code >= 500 else "http"
                    err = CoolapkError(
                        f"HTTP {code}",
                        error_code=code,
                        kind=kind,
                        retry_after=retry_after,
                        retryable=code >= 500,
                    )
            except (httpx.HTTPError, httpx.InvalidURL) as e:
                # 兜底：ReadError/RemoteProtocolError/ProxyError 等散装 httpx 异常
                # （以及不属于 HTTPError 家族的 InvalidURL）统一归入 network 走重试，
                # 文案带异常类名，便于区分「连接被重置」还是「URL 非法」。
                # asyncio.CancelledError 继承 BaseException，不会被这层接住。
                last_status = None
                err = CoolapkError(
                    f"请求异常({type(e).__name__}): {e}", kind="network", retryable=True
                )
            if err is not None and not err.retryable:
                # 不可重试（http/risk）/可直接上报（auth_expired）的错误立即抛出，
                # 不再白白消耗退避配额
                raise err
            last_err = err
            if attempt < throttle.MAX_RETRIES:
                # 429/5xx 带 Retry-After 时按服务端要求睡够（受退避上限约束）
                await throttle.backoff_sleep(attempt, floor=err.retry_after or 0.0)

        if last_err is not None:
            # 重试耗尽仍失败：瞬态错误（超时/连接/5xx/429）重试后依旧不成功。
            # raise_for_status 抛错时 resp 已赋值，不能靠 resp is None 判定；
            # 按末次状态码重抛，不再解析可能是 HTML 错误页的 body（否则会被
            # resp.json() 误分类成 kind=parse，丢失可重试语义）
            raise CoolapkError(
                f"{last_err.message}（重试 {throttle.MAX_RETRIES} 次后仍失败）",
                error_code=last_status if last_status is not None else -1,
                kind=last_err.kind,
                retry_after=last_err.retry_after,
                retryable=last_err.retryable,
            )
        if resp is None:
            raise CoolapkError("请求失败", kind="network")

        try:
            result = resp.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            # 风控页/网关错误页常返回 HTML；编码异常按同类处理
            raise CoolapkError("响应解析失败", kind="parse") from None

        # 检查业务错误。V6 错误响应两种形态：
        # ① 顶层 {error, message, ...}（无 data 键）；
        # ② {status: 非0, message, data: null}（风控/网络环境异常，status 字段必须读，
        #    只看「无 data 键」会把 status=1004/-415 的风控响应当成功，data=None
        #    还会被写进缓存造成缓存投毒）
        status_code = result.get("status") if isinstance(result, dict) else None
        if isinstance(status_code, bool):
            # 布尔 status：False==0 会混进 (0,1) 合法集被当成功；归一成 int，
            # False 视为失败信号（-1）、True 视为成功（1）
            status_code = 1 if status_code else -1
        failed = (
            not isinstance(result, dict)
            or ("data" not in result and "message" in result)
            or (status_code is not None and status_code not in (0, 1, "0", "1"))
        )
        if failed and isinstance(result, dict):
            error_code = result.get("error", -1)
            message = result.get("message", "未知错误")
            if isinstance(error_code, str):
                error_code = int(error_code) if error_code.lstrip("-").isdigit() else -1
            if status_code is not None and status_code not in (0, 1, "0", "1"):
                # status 字段优先：1004=网络环境异常（风控）、-415=设备码被拉黑
                if isinstance(status_code, str) and status_code.lstrip("-").isdigit():
                    status_code = int(status_code)
                if isinstance(status_code, int):
                    error_code = error_code if error_code != -1 else status_code
            # 403/captchaRequired 是登录态失效，必须先于「验证码」判定——
            # 否则 message 里的 captchaRequired 会被 "captcha" 子串抢判成图形验证码
            if error_code == 403 or "captchaRequired" in str(result):
                raise CoolapkError(AUTH_EXPIRED_MESSAGE, error_code=403, kind="auth_expired")
            if "验证码" in message:
                # 负缓存：该端点短期内已被风控，冷却期内不再请求（base 在函数开头算好）
                self._blocked[base] = time.monotonic() + self.CAPTCHA_COOLDOWN
                raise CoolapkError(
                    message, error_code, kind="captcha", retry_after=self.CAPTCHA_COOLDOWN,
                )
            if error_code == -415:
                raise CoolapkError(
                    f"设备码被风控拉黑({message})，请更换登录态设备码", error_code, kind="api",
                )
            if error_code == 1004:
                raise CoolapkError(
                    f"请求被风控拦截({message})，请稍后再试", error_code, kind="api",
                )
            raise CoolapkError(message, error_code)
        if not isinstance(result, dict):
            # 部分接口（如搜索、话题动态）顶层直接返回 list，是预期形态
            data = result
        else:
            data = result.get("data")
        if isinstance(data, list):
            self._index_feeds(data)
        # data 为 None 视为失败残余，不写入缓存，避免风控响应污染缓存
        if cache_key is not None and data is not None:
            self._cache.put(cache_key, data, throttle.ttl_for(path))
        return data

    def _endpoint_blocked(self, base: str) -> bool:
        until = self._blocked.get(base, 0.0)
        if until and time.monotonic() < until:
            return True
        self._blocked.pop(base, None)
        return False

    async def get(self, path: str, params: dict | None = None) -> Any:
        return await self._request("GET", path, params=params)

    # post/post_multipart 仅登录流程（login --sms 等）内部使用，
    # MCP 工具层与 get_url 均不触达，属于客户端必需的底层能力而非写接口暴露
    async def post(self, path: str, data: dict | None = None) -> Any:
        return await self._request("POST", path, data=data or {})

    async def post_multipart(
        self, path: str, fields: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> Any:
        """multipart/form-data POST。files 格式: {name: (filename, content, content_type)}"""
        return await self._request(
            "POST", path, data=fields or {}, files=files or {}
        )

    # ---- 只读透传与图片下载（MCP 工具专用，不用于业务写入） ----

    @classmethod
    def _check_get_url_path(cls, path: str) -> str:
        """校验 get_url 的路径参数：仅允许 api.coolapk.com 的 GET /v6/ 只读路径。

        返回规范化后的路径；违规抛 CoolapkError(error_code=403)。
        """
        path = (path or "").strip()
        # 允许整段 URL 输入，但必须是 api.coolapk.com
        if path.startswith(("http://", "https://")):
            from urllib.parse import urlsplit
            host = urlsplit(path).netloc.lower()
            if host not in ("api.coolapk.com", "api2.coolapk.com"):
                raise CoolapkError(
                    f"拒绝访问非酷安 API 域名: {host}（仅允许 api.coolapk.com）",
                    error_code=403, kind="api",
                )
            path = urlsplit(path).path + (
                f"?{urlsplit(path).query}" if urlsplit(path).query else ""
            )
        if not path.startswith("/"):
            path = f"/{path}"
        # fragment 不进请求行：httpx 发请求时会把它整段丢掉，而守卫是在含 # 的
        # 原始串上做段匹配 —— 不先剥掉就会出现「守卫看到 like#x、线上发出 like」
        # 的绕过（/v6/feed/like#x 曾是放行的）。剥完再重建，保证校验串=发送串。
        if "#" in path:
            path = path.split("#", 1)[0]
        if not path.startswith("/v6/"):
            raise CoolapkError(
                f"仅允许 /v6/ 开头的接口路径，收到: {path}",
                error_code=403, kind="api",
            )
        # 写端点黑名单：按「query 之前的路径部分」的独立段匹配，拒绝写动作端点。
        # （酷安写接口全部走 GET 传参，必须拦在客户端；docs/coolapk-api.md:574
        # /v6/feed/like 即 GET。）不能裸子串匹配——/v6/feed/replyList 含 "/reply"、
        # /v6/message-like 含 "/message" 这类只读端点会被误伤；
        # 校验前先循环 unquote 防双重编码绕过（%64igg、%2564igg）。
        from urllib.parse import unquote

        decoded = path
        for _ in range(3):
            unquoted = unquote(decoded)
            if unquoted == decoded:
                break
            decoded = unquoted
        lowered = decoded.lower()
        path_part = lowered.split("?", 1)[0]
        segs = [seg for seg in path_part.strip("/").split("/") if seg]
        # 拒绝点段、控制字符、反斜杠和分号矩阵参数：部分反代或上游会先
        # 归一化路径，"/v6/feed/./like" 会被还原成写端点 "/v6/feed/like"；
        # 分号可能被当成 matrix parameter 剥掉，控制字符可能被截断处理，
        # 都会让下面的段匹配黑名单失效。
        if (
            "\\" in path_part
            or ";" in path_part
            or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path_part)
            or any(seg in (".", "..") for seg in segs)
        ):
            raise CoolapkError(
                f"拒绝含点段/控制字符/反斜杠/分号的路径: {path}",
                error_code=403, kind="api",
            )
        # account 模块整组敏感（登录/验证码/token 下发/风控），透传只放行
        # 登录态检查与风控解锁两个端点；内部业务调用不走本校验，不受影响
        if len(segs) >= 2 and segs[0] == "v6" and segs[1] == "account":
            if segs[2:] not in (["checklogininfo"], ["requestvalidate"]):
                raise CoolapkError(
                    f"拒绝访问 account 模块端点: {path}（仅放行 checkLoginInfo/requestValidate）",
                    error_code=403, kind="api",
                )
        # 单段 + 相邻两段拼接两种形态：/v6/reply/add 这类写端点
        # 靠 "/reply/add" 两段项命中，且不会误伤单段只读端点（replyList/message-like）
        single_segments = {f"/{s}" for s in segs}
        pair_segments = {f"/{segs[i]}/{segs[i+1]}" for i in range(len(segs) - 1)}
        # 单段黑名单：确切的写动作词。exact 匹配不伤子串（followtag≠follow、
        # replyList 不含 /reply），但没有同名只读端点的泛词必须排除在单段之外——
        # /vote /collection /favorite /blacklist 都是只读模块名
        # （/v6/vote/commentList、/v6/collection/list、/v6/favorite/list、
        # /v6/user/blackList），/like 撞只读的 /v6/notification/like
        blocked_segments = (
            "/digg", "/replyadd", "/replyaddpush",
            "/follow", "/unfollow", "/followuser", "/unfollowuser",
            "/followtag", "/unfollowtag",
            "/createfeed", "/publish", "/deletefeed", "/deletecomment",
            "/delete", "/edit", "/update", "/sendmessage", "/upload", "/report",
            "/block", "/subscribe", "/checkin", "/sign", "/apply", "/modify", "/set",
            "/unfavorite", "/sendvcode",
            "/addtoblacklist", "/removefromblacklist",
            "/addtoignorelist", "/removefromignorelist",
        )
        for seg in blocked_segments:
            if seg in single_segments:
                raise CoolapkError(
                    f"拒绝写操作端点: {path}",
                    error_code=403, kind="api",
                )
        # 泛词写端点按「资源/动作」相邻两段拦：like/unlike 的宿主资源不枚举全，
        # 只拦已知的写端点形态；/notification/like 这类只读端点不在列，不受影响
        blocked_pairs = (
            "/reply/add", "/reply/delete", "/reply/edit",
            "/message/send", "/message/delete",
            "/feed/like", "/feed/unlike",
            "/reply/like", "/reply/unlike",
            "/collection/like", "/collection/unlike",
            "/feed/vote", "/feed/favorite", "/apk/favorite",
            "/collection/follow", "/collection/unfollow",
        )
        for pair in blocked_pairs:
            if pair in pair_segments:
                raise CoolapkError(
                    f"拒绝写操作端点: {path}",
                    error_code=403, kind="api",
                )
        # 严格模式：黑名单之上再加正向白名单（末段形态像只读端点才放行）。
        # 默认开启：黑名单只能拦已知写端点，接口新上的写动作必须靠正向白名单兜住；
        # 需要透传非常规只读路径时设 COOLAPK_GET_URL_STRICT=0 临时关闭。
        if cls.GET_URL_STRICT and not _looks_read_only(path_part):
            raise CoolapkError(
                f"严格模式拒绝非常规只读形态的路径: {path}"
                f"（仅放行 detail/list/info/space 等只读末段；"
                f"如需透传请设 COOLAPK_GET_URL_STRICT=0）",
                error_code=403, kind="api",
            )
        return path

    async def get_url(self, path: str) -> Any:
        """只读透传：GET 任意 /v6/ 只读路径并返回原始 JSON。

        仅允许 api.coolapk.com 域名；写端点黑名单一律 403 拒绝。
        """
        return await self.get(self._check_get_url_path(path))

    @staticmethod
    def _thumb_fetch_url(url: str) -> str | None:
        """带 @WxH 尺寸后缀的 jpg 返回 .m.jpg 缩略地址，否则 None。

        受 COOLAPK_IMG_THUMBNAIL 开关控制（默认开，"0"/"false" 关）。
        缩略长边约 1080px，仍在主流多模态模型视觉窗口内，读图内容不受影响。
        """
        if os.environ.get("COOLAPK_IMG_THUMBNAIL", "1").lower() in ("0", "false"):
            return None
        parts = urlsplit(url)
        if parts.path.endswith(".jpg") and "@" in parts.path:
            # 缩略后缀必须加在 path 尾部再重组：直接 url + ".m.jpg" 会让后缀落进
            # query（...jpg?x=1.m.jpg），CDN 拿不到缩略图
            return urlunsplit(parts._replace(path=parts.path + ".m.jpg"))
        return None

    async def download_image(self, url: str) -> tuple[bytes, str]:
        """下载 CDN 图片，返回 (图片字节, 格式名)。

        酷安图片 CDN 校验 Referer：必须带 https://www.coolapk.com/，
        用浏览器 UA 即可放行（实测无需 App UA，见 src-tauri client.rs:3790）。
        仅允许 image/avatar/static.coolapk.com 三个 CDN 域名。
        """
        from urllib.parse import urlsplit

        url = (url or "").strip()
        if url.startswith("//"):
            url = f"https:{url}"
        host = urlsplit(url).netloc.lower()
        # 用 hostname+端口判定（netloc 含 userinfo 与端口，image.coolapk.com@evil.com
        # 这类混淆在 netloc 比较下会误判；重定向后的每一跳也走同一判定）
        if not _image_url_ok(url):
            raise CoolapkError(
                f"图片域名不在白名单: {host or url}"
                f"（仅允许 {'/'.join(sorted(_IMAGE_HOSTS))}）",
                error_code=403, kind="api",
            )
        if url.startswith("http://"):
            url = f"https://{url[len('http://'):]}"
        # 缩略优先（与 /img 中转同一开关 COOLAPK_IMG_THUMBNAIL，默认开）：
        # 上游非 200 自动回落原图。
        fetch_url = self._thumb_fetch_url(url) or url
        thumbnail_used = fetch_url != url

        headers = {"User-Agent": _IMAGE_UA, "Referer": "https://www.coolapk.com/"}
        status, data, ctype = await _fetch_image_bytes(fetch_url, headers)
        if thumbnail_used and status != 200:
            # 缩略不存在（404/567 等）：回落原图
            status, data, ctype = await _fetch_image_bytes(url, headers)
            thumbnail_used = False

        if status != 200:
            raise CoolapkError(
                f"图片下载失败: HTTP {status}（URL 可能已失效，"
                f"agent 可改用 coolapk_get_url 获取接口数据，或直接向用户呈现 URL）",
                error_code=status, kind="http",
            )
        if not data:
            raise CoolapkError("图片下载失败: 响应体为空", kind="http")

        # 从 Content-Type 或 URL 扩展名推断格式（FastMCP Image 需要）。
        # FastMCP 直接 f"image/{format}" 拼 MIME，必须用标准名 "jpeg"，
        # "jpg" 会生成非法的 image/jpg 被客户端拒绝渲染。
        ctype = (ctype or "").lower()
        for fmt in ("webp", "jpeg", "jpg", "png", "gif"):
            if fmt in ctype:
                return data, "jpeg" if fmt in ("jpeg", "jpg") else fmt
        lower_url = url.lower().split("?", 1)[0]
        for ext in (".webp", ".jpg", ".jpeg", ".png", ".gif"):
            if lower_url.endswith(ext):
                return data, "jpeg" if ext in (".jpg", ".jpeg") else ext[1:]
        # 未见扩展名时按魔数判断
        if data.startswith(b"\x89PNG"):
            return data, "png"
        if data.startswith(b"\xff\xd8"):
            return data, "jpeg"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return data, "gif"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return data, "webp"
        # 未知魔数默认 jpeg：HTTP 抓到的图绝大多数是 jpg，png 会生成
        # 错误的 image/png MIME 导致客户端解码失败
        return data, "jpeg"

    # ---- 浏览 ----

    async def get_home_feeds(self, page: int = 1) -> list[dict]:
        """首页推荐动态"""
        return await self.get(
            "/v6/page/dataList",
            {"url": "/page?url=V9_HOME_TAB_FOLLOW", "page": page},
        )

    async def get_hot_feeds(self, page: int = 1) -> list[dict]:
        """热门动态"""
        return await self.get(
            "/v6/page/dataList",
            {"url": "/feed/hotList", "page": page},
        )

    async def get_latest_feeds(self, page: int = 1) -> list[dict]:
        """最新动态"""
        return await self.get(
            "/v6/page/dataList",
            {"url": "/feed/newestList", "page": page},
        )

    async def get_main_feeds(self, page: int = 1) -> list[dict]:
        """首页推荐（main/indexV8）"""
        return await self.get("/v6/main/indexV8", {"page": page})

    async def get_board_feeds(
        self, board_id_or_url: str, page: int = 1
    ) -> list[dict]:
        """通用板块浏览

        Args:
            board_id_or_url: 板块 ID（如 digital）、page_name（如 V10_DIGITAL_HOME）、或完整 url
            page: 页码
        """
        from coolapk_mcp.boards import BOARD_URLS, resolve_board_url

        url = resolve_board_url(board_id_or_url, BOARD_URLS)
        return await self.get("/v6/page/dataList", {"url": url, "page": page})

    async def get_secondhand_feeds(
        self, brand: int | None = None, page: int = 1
    ) -> list[dict]:
        """二手交易帖子

        Args:
            brand: 品牌ID（1005=苹果），None 则全部二手
            page: 页码
        """
        if brand:
            url = f"#/feed/ershouList?brand={brand}&dataListType=staggered"
        else:
            url = "#/feed/ershouList?dataListType=staggered"
        return await self.get("/v6/page/dataList", {"url": url, "page": page})

    # ---- 动态 ----

    async def get_feed_detail(self, feed_id: int) -> dict | list:
        """动态详情"""
        return await self.get(f"/v6/feed/detail?id={feed_id}")

    async def get_feed_replies(
        self, feed_id: int, page: int = 1, list_type: str = "lastupdate_desc"
    ) -> list[dict]:
        """动态回复列表"""
        return await self.get(
            f"/v6/feed/replyList?id={feed_id}&listType={list_type}"
            f"&page={page}&discussMode=1&feedType=feed"
        )

    async def get_hot_replies(self, feed_id: int, page: int = 1) -> list[dict]:
        """热门回复"""
        return await self.get(
            f"/v6/feed/hotReplyList?id={feed_id}&page={page}&discussMode=1"
        )

    # ---- 分页收集：预算闸门 + 去重 + 截断标记 ----

    @staticmethod
    def _ensure_budget(budget: Budget | None, max_pages: int) -> Budget:
        """预算缺省时按页数上限建默认预算：调用方不传也不会无限翻页。"""
        if budget is not None:
            return budget
        return Budget(max_requests=max(1, int(max_pages)), deadline=30.0)

    @staticmethod
    def _dedupe_key(item: dict) -> tuple[str, str] | None:
        """去重键：优先 id；缺 id 时退回标题；两者都没有则不去重（返回 None）。"""
        rid = item.get("id")
        if rid is not None and str(rid) != "":
            return ("id", str(rid))
        title = item.get("title")
        if isinstance(title, str) and title:
            return ("title", title)
        return None

    @staticmethod
    async def _fetch_page_within(
        budget: Budget, page: int, fetch_page: Callable[[int], Awaitable[Any]]
    ) -> Any:
        """在预算剩余时间内取一页。

        没有这层时 deadline 只约束「页与页之间」：单页内部还要走限速、重试与
        httpx 30s 超时，focus 的 25s 预算实际会拖到 30s 以上才返回。超时按可
        重试的 network 错误抛出，由 _collect_paged 决定保留已取页还是上抛。
        """
        remaining = budget.remaining()
        try:
            return await asyncio.wait_for(fetch_page(page), timeout=max(1.0, remaining))
        except asyncio.TimeoutError:
            raise CoolapkError(
                f"第 {page} 页超过时间预算（剩余 {remaining:.0f}s）",
                kind="network",
                retryable=True,
            ) from None


    async def _collect_paged(
        self,
        fetch_page: Callable[[int], Awaitable[Any]],
        *,
        max_pages: int,
        start_page: int = 1,
        budget: Budget | None = None,
    ) -> FetchResult:
        """通用翻页：每页请求前过预算闸门，按 id/标题去重，收尾标记截断。

        截断语义（server 端按此消费）：
        - 预算耗尽：truncated=True，next_page=还没发出去的那一页；
        - 跑满 max_pages 且末页仍满（>= PAGE_SIZE 条）：truncated=True，next_page=下一页；
        - 空页 / 末页不足 PAGE_SIZE 条 / 整页重复：确实到底，truncated=False。

        中途某页抛 CoolapkError 时保留已取内容并把该页标成可续抓；requests 按
        本次真实发出的页请求数计（budget 被多路共享时也不重复计）。
        """
        budget = self._ensure_budget(budget, max_pages)
        pages = max(1, int(max_pages))
        items: list[dict] = []
        seen: set[tuple[str, str]] = set()
        truncated = False
        next_page: int | None = None
        page = max(1, int(start_page))
        taken = 0
        for _ in range(pages):
            try:
                budget.take()
            except BudgetExceeded:
                # 预算耗尽：已取部分照常返回，从当前页续抓
                truncated, next_page = True, page
                break
            taken += 1
            try:
                batch = await self._fetch_page_within(budget, page, fetch_page)
            except CoolapkError as exc:
                # 某一页失败：前面已取到的必须保留下来。丢掉它们等于把「有 30 条
                # 评论」变成「没有评论」，用户完全无法察觉。整批为空时才往上抛。
                if not items:
                    raise
                logger.warning("翻页在第 %s 页失败，保留已取 %s 条：%s", page, len(items), exc)
                truncated, next_page = True, page
                break
            if not isinstance(batch, list) or not batch:
                break
            fresh = 0
            for item in batch:
                if not isinstance(item, dict):
                    continue
                key = self._dedupe_key(item)
                if key is not None:
                    if key in seen:
                        continue
                    seen.add(key)
                items.append(item)
                fresh += 1
            if fresh == 0:
                # 整页都是重复（置顶帖重复页等），说明已经翻到底
                break
            if len(batch) < self.PAGE_SIZE:
                # 末页不足一屏：到底了
                break
            page += 1
        else:
            # 跑满 max_pages 且末页仍满：可能还有下一页
            truncated, next_page = True, page
        return FetchResult(
            items,
            truncated=truncated,
            next_page=next_page,
            requests=taken,
        )

    async def get_author_replies_paged(
        self, feed_id: int, max_pages: int = 5, budget: Budget | None = None,
        start_page: int = 1,
    ) -> FetchResult:
        """只看楼主：多页聚合楼主回复（去重 + 预算控制）。

        start_page 供 focus.next_page 续抓：调用方拿到 next_page 后下次从该页起。
        """
        return await self._collect_paged(
            lambda page: self.get_author_replies(feed_id, page),
            max_pages=max_pages,
            budget=budget,
            start_page=start_page,
        )

    async def get_popular_replies_paged(
        self, feed_id: int, max_pages: int = 5, budget: Budget | None = None,
        start_page: int = 1,
    ) -> FetchResult:
        """热门评论：多页聚合高赞评论（去重 + 预算控制 + 可续抓页码）。"""
        return await self._collect_paged(
            lambda page: self.get_popular_replies(feed_id, page),
            max_pages=max_pages,
            budget=budget,
            start_page=start_page,
        )

    async def get_author_replies(self, feed_id: int, page: int = 1) -> list[dict]:
        """只看楼主：拉取楼主在评论区的回复（含楼中楼内联）。

        酷安工具帖的关键补充信息（获取方式、答疑、密码说明）多在楼主回复里，
        fromFeedAuthor=1 能一次精准命中，不用翻全部评论。
        """
        return await self.get(
            f"/v6/feed/replyList?id={feed_id}&listType=&page={page}"
            f"&discussMode=1&feedType=feed&blockStatus=0&fromFeedAuthor=1"
        )

    async def get_popular_replies(self, feed_id: int, page: int = 1) -> list[dict]:
        """热门评论（listType=popular，按点赞排序）。

        高赞评论多为亲测反馈 / 踩坑指南，信息密度远高于默认时间序。
        """
        return await self.get(
            f"/v6/feed/replyList?id={feed_id}&listType=popular&page={page}"
            f"&discussMode=1&feedType=feed&blockStatus=0&fromFeedAuthor=0"
        )

    async def get_sub_replies_paged(
        self, reply_id: int, page: int = 1, max_pages: int = 5,
        budget: Budget | None = None,
    ) -> FetchResult:
        """楼中楼完整翻页（feedType=feed_reply + firstItem/lastItem 游标）。

        服务端默认只在父评论里内联最近几条楼中楼；作者答疑常在楼中楼深处，
        此方法按游标翻页补全。返回去重后的子回复 + truncated/next_page/requests。
        """
        pages = max(1, int(max_pages))
        budget = self._ensure_budget(budget, pages)
        used_before = budget.used
        collected: list[dict] = []
        seen: set[tuple[str, str]] = set()
        first_item = ""
        last_item = ""
        truncated = False
        next_page: int | None = None
        current = max(1, int(page))
        for _ in range(pages):
            try:
                budget.take()
            except BudgetExceeded:
                truncated, next_page = True, current
                break
            params = (
                f"/v6/feed/replyList?id={reply_id}&listType=&page={current}"
                f"&discussMode=1&feedType=feed_reply&blockStatus=0&fromFeedAuthor=0"
            )
            if first_item:
                params += f"&firstItem={first_item}"
            if last_item:
                params += f"&lastItem={last_item}"
            batch = await self.get(params)
            if not isinstance(batch, list) or not batch:
                break
            fresh = 0
            for item in batch:
                if not isinstance(item, dict):
                    continue
                key = self._dedupe_key(item)
                if key is not None:
                    if key in seen:
                        continue
                    seen.add(key)
                collected.append(item)
                fresh += 1
            first_item = str(batch[0].get("id", "")) if isinstance(batch[0], dict) else ""
            last_item = str(batch[-1].get("id", "")) if isinstance(batch[-1], dict) else ""
            if fresh == 0 or len(batch) < self.PAGE_SIZE:
                break
            current += 1
        else:
            # 跑满 max_pages 且末页仍满：可能还有下一页
            truncated, next_page = True, current
        return FetchResult(
            collected,
            truncated=truncated,
            next_page=next_page,
            requests=budget.used - used_before,
        )

    async def get_feed_history(self, feed_id: int, page: int = 1) -> list[dict]:
        """动态修改历史：作者每次编辑正文留下的快照。

        版本考古用——能看到教程帖 v1.0→v2.2 的正文演进（支持机型、获取方式变化）。
        """
        return await self.get(f"/v6/feed/changeHistoryList?id={feed_id}&page={page}")

    async def get_all_replies(
        self, feed_id: int, max_pages: int = 30, budget: Budget | None = None
    ) -> FetchResult:
        """拉取全部一级评论（自动翻页去重；楼中楼已内联在每条的 replyRows 里）。

        max_pages 是安全上限，防止超大帖（数千评论）把请求预算耗尽；
        预算耗尽或超过上限时返回已取到的部分，truncated/next_page 指明截断位置。
        默认上限可用环境变量 COOLAPK_MAX_REPLY_PAGES 调整（1-100）。
        """
        if max_pages == 30:
            try:
                env_val = int(os.environ.get("COOLAPK_MAX_REPLY_PAGES", "") or 30)
                max_pages = max(1, min(100, env_val))
            except (TypeError, ValueError):
                pass
        return await self._collect_paged(
            lambda page: self.get_feed_replies(feed_id, page),
            max_pages=max_pages,
            budget=budget,
        )

    async def get_feed_detail_smart(
        self,
        feed_id: int,
        author_uid: int | None = None,
        with_replies: bool = True,
        reply_page: int = 1,
        all_replies: bool = False,
        budget: Budget | None = None,
    ) -> dict:
        """动态详情（带风控回落）

        /v6/feed/detail 被验证码拦截时，自动改从作者动态列表取帖子全文；
        回复走独立的 replyList 接口（通常不受详情接口风控影响）。

        Args:
            feed_id: 帖子ID
            author_uid: 作者UID（提供则回落更快；不提供则查本地 feed_id→uid 索引）
            with_replies: 是否带回复
            reply_page: 回复页码
            budget: 请求预算。传入时与作者动态回落扫描、all_replies 翻页共享同一
                份配额；None 时回落扫描自建小预算，all_replies 由 get_all_replies
                按 max_pages 自建默认预算

        Returns:
            {"source": "api"|"author_feed_fallback", "data": dict, "replies": list|None}
        """
        detail_raw = None
        source = "api"
        if not self._endpoint_blocked("/v6/feed/detail"):
            try:
                raw = await self.get_feed_detail(feed_id)
                if isinstance(raw, list) and raw:
                    raw = raw[0]
                if isinstance(raw, dict):
                    detail_raw = raw
            except CoolapkError as e:
                if e.kind != "captcha":
                    raise

        if detail_raw is None:
            uid = author_uid or self._uid_index.get(feed_id)
            if not uid:
                raise CoolapkError(
                    "帖子详情被验证码拦截，且无作者UID可回落；请提供 author_uid 或先搜索该帖",
                    kind="captcha",
                )
            # 回落扫描同样受预算约束：调用方传入时与回复翻页共享配额；
            # 未传时只建一个刚好够扫 FALLBACK_SCAN_PAGES 页的小预算，不无限请求
            scan_budget = budget if budget is not None else Budget(
                max_requests=self.FALLBACK_SCAN_PAGES, deadline=30.0
            )
            for page in range(1, self.FALLBACK_SCAN_PAGES + 1):
                try:
                    scan_budget.take()
                except BudgetExceeded:
                    # 预算耗尽：不再扫，按「未找到」走下面的 captcha 错误
                    break
                feeds = await self.get_user_feeds(uid, page)
                if not isinstance(feeds, list):
                    break
                for item in feeds:
                    if isinstance(item, dict) and str(item.get("id")) == str(feed_id):
                        detail_raw = item
                        break
                if detail_raw is not None or len(feeds) < self.PAGE_SIZE:
                    break
            if detail_raw is None:
                raise CoolapkError(
                    f"帖子详情被验证码拦截，且在作者(uid={uid})前"
                    f"{self.FALLBACK_SCAN_PAGES}页动态中未找到 id={feed_id}",
                    kind="captcha",
                )
            source = "author_feed_fallback"

        replies: Any = None
        if with_replies:
            try:
                if all_replies:
                    replies = await self.get_all_replies(feed_id, budget=budget)
                else:
                    replies = await self.get_feed_replies(feed_id, reply_page)
            except CoolapkError as exc:
                # 不能降成 None：server 会把 None 当成「这条帖子没有评论」返回，
                # 用户看到的是「0 条评论」而非「评论没取到」。空 FetchResult 带
                # truncated=True 才能让上层如实告知并可续抓。
                logger.warning("回复列表获取失败（feed %s）：%s", feed_id, exc)
                replies = FetchResult([], truncated=True, next_page=reply_page)

        return {"source": source, "data": detail_raw, "replies": replies}

    # ---- 搜索 ----

    async def search(
        self,
        keyword: str,
        search_type: str = "feed",
        page: int = 1,
        sort: str = "default",
    ) -> list[dict]:
        """通用搜索
        search_type: feed / user / feedTopic / app
        sort: feed 专用排序，hot(热门)/reply(回复数)/default(综合)；
            实测 hot 显著提升结果质量（默认排序多为低互动点评）
        """
        params: dict[str, Any] = {
            "type": search_type,
            "searchValue": keyword,
            "page": page,
            "showAnonymous": "-1",
        }
        if search_type == "feed":
            params["sort"] = sort
        return await self.get("/v6/search", params)

    async def search_feeds(
        self,
        keyword: str,
        feed_type: str = "feed",
        sort: str = "default",
        page: int = 1,
    ) -> list[dict]:
        """搜索动态"""
        return await self.get(
            "/v6/search",
            {
                "type": "feed",
                "feedType": feed_type,
                "sort": sort,
                "searchValue": keyword,
                "page": page,
                "showAnonymous": "-1",
            },
        )

    # ---- 用户 ----

    async def get_user_space(self, uid: int) -> dict | list:
        """用户空间"""
        return await self.get(f"/v6/user/space?uid={uid}")

    async def get_user_profile(self, uid: int) -> dict | list:
        """用户资料"""
        return await self.get(f"/v6/user/profile?uid={uid}")

    async def get_user_feeds(
        self, uid: int, page: int = 1, feed_type: str = "feed"
    ) -> list[dict]:
        """用户动态列表"""
        return await self.get(
            f"/v6/user/{feed_type}List?uid={uid}&page={page}&isIncludeTop=1"
        )

    async def get_user_feeds_pages(
        self, uid: int, pages: int = 2, start_page: int = 1, feed_type: str = "feed",
        budget: Budget | None = None,
    ) -> FetchResult:
        """用户动态多页聚合，按 id 去重（预算 + 截断标记）。

        置顶帖带 isIncludeTop=1 时每页都会带一次（实测 3 页 60 条里只有 1 条重复），
        所以去重是必需的。首页失败照常抛错；后续页失败只保留已抓到的部分，
        避免因为翻到底或被限流把整次聚合废掉。预算耗尽或跑满页数上限时
        truncated=True 并给出 next_page（动态页无稳定游标，用页码续抓）。
        """
        pages = max(1, min(int(pages or 1), 5))
        start = max(1, int(start_page or 1))
        budget = self._ensure_budget(budget, pages)
        seen: set[tuple[str, str]] = set()
        out: list[dict] = []
        truncated = False
        next_page: int | None = None
        page = start
        taken = 0
        for offset in range(pages):
            page = start + offset
            try:
                budget.take()
            except BudgetExceeded:
                truncated, next_page = True, page
                break
            taken += 1
            try:
                items = await self.get_user_feeds(uid, page, feed_type)
            except CoolapkError:
                if offset == 0:
                    raise
                # 后续页失败：保留已抓部分，并明确标「没翻到底」，否则调用方
                # 会把「第 3 页 500」当成「这个用户只有两页动态」
                truncated, next_page = True, page
                break
            if not isinstance(items, list) or not items:
                break
            fresh = 0
            for item in items:
                if not isinstance(item, dict):
                    continue
                key = self._dedupe_key(item)
                if key is not None:
                    if key in seen:
                        continue
                    seen.add(key)
                out.append(item)
                fresh += 1
            if fresh == 0:
                # 整页都是重复，说明已经翻到底（置顶帖重复页）
                break
            if len(items) < self.PAGE_SIZE:
                break
        else:
            # 跑满页数上限且末页仍满：还有更早的动态可翻
            truncated, next_page = True, page + 1
        return FetchResult(
            out,
            truncated=truncated,
            next_page=next_page,
            requests=taken,
        )

    # ---- 话题 ----

    async def get_topic_detail(self, tag: str) -> dict | list:
        """话题详情"""
        # tag 必须显式 quote：话题名常含 #（如 #小米15#），不编码会被
        # httpx 当 fragment 截断，query 实际发送为 tag=（空值）
        return await self.get(f"/v6/topic/newTagDetail?tag={quote(tag, safe='')}")

    async def get_topic_feeds(self, tag: str, page: int = 1) -> list[dict]:
        """话题下的帖子"""
        return await self.get(
            f"/v6/topic/tagFeedList?tag={quote(tag, safe='')}&page={page}"
        )

    async def get_topic_suggest(self, keyword: str) -> list[dict]:
        """搜索联想词（查询改写积木：返回酷安建议的搜索词卡片）"""
        return await self.get(
            f"/v6/search/suggestSearchWordsNew?searchValue={quote(keyword, safe='')}"
        )

    # ---- 应用 ----

    async def get_app_detail(self, package_name: str) -> dict | list:
        """应用详情"""
        return await self.get(f"/v6/apk/detail?id={package_name}")

    # ---- 通知 ----

    async def get_notification_count(self) -> dict | list:
        """未读通知计数"""
        return await self.get("/v6/notification/checkCount")

    async def get_notifications(
        self, ntype: str, page: int = 1
    ) -> list[dict]:
        """通知列表
        ntype: atMeMeFeed / atComment / comment / feedLike / contactsFollow / message
        """
        return await self.get(f"/v6/notification/{ntype}?page={page}")

    # ---- 登录态校验 ----

    async def check_login(self) -> Any:
        """校验当前登录态：本地检查不通过直接抛 auth_expired；通过则调 checkLoginInfo 确认

        返回 {"logged_in": True, "uid": ..., "username": ..., "source": ...}。
        """
        self._check_local_auth()
        await self.get("/v6/account/checkLoginInfo")
        return {
            "logged_in": True,
            "uid": self.config.uid,
            "username": self.config.username,
            "source": self.config.source,
        }

    # ---- 验证码 ----

    async def request_validate(self, captcha: str, **extra) -> Any:
        """提交图形验证码校验（风控解锁）

        Args:
            captcha: 用户输入的验证码
            **extra: 额外参数（视风控场景而定）
        """
        data = {"captcha": captcha, **extra}
        return await self.post("/v6/account/requestValidate", data)

    # ---- SMS 登录（走 account.coolapk.com 网页登录流程） ----

    async def _auth_request(
        self, method: str, path: str, sessid: str = "",
        data: dict | None = None, referer: str = "https://account.coolapk.com/auth/login?type=mobile",
    ) -> httpx.Response:
        """account.coolapk.com 的请求（模拟酷安 APP WebView）

        关键点：
        - User-Agent 用真实 Android WebView UA（Chrome 149）
        - X-Requested-With: com.coolapk.market 标识 APP
        - Cookie 带 DID/oaid/AppSupported 等 APP 设备标识（服务端校验是否来自 APP）
        - 不自动跟随重定向（follow_redirects=False），调用方需自行处理 302
        """
        url = f"https://account.coolapk.com{path}"
        # 组装 Cookie：SESSID + APP 设备标识
        cookie_parts = []
        if sessid:
            cookie_parts.append(sessid)
        cookie_parts.append("forward=https://www.coolapk.com")
        cookie_parts.append("AppSupported=2604201")
        cookie_parts.append("darkMode=0")
        cookie_parts.append("displayVersion=v14")
        # DID 和 oaid 是 APP 设备标识，validateLogin 端点用这些判断是否来自 APP
        # 这些值从手机 ADB 提取或用任意值（服务端不严格校验具体值，只检查字段存在）
        cookie_parts.append("DID=coolapk_mcp_default_did")
        cookie_parts.append("oaid=coolapk_mcp_default_oaid")
        cookie_str = "; ".join(cookie_parts)

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 16; 23113RKC6C Build/BP2A.250605.031.A3; wv) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
                "Chrome/149.0.7827.91 Mobile Safari/537.36"
            ),
            "X-Requested-With": "com.coolapk.market",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": referer,
            "sec-ch-ua": '"Android WebView";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
            "sec-ch-ua-mobile": "?1",
            "sec-ch-ua-platform": '"Android"',
            "Cookie": cookie_str,
        }
        if method == "POST":
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Origin"] = "https://account.coolapk.com"
            headers["X-Requested-With"] = "XMLHttpRequest"
            headers["Accept"] = "*/*"
            headers["Sec-Fetch-Site"] = "same-origin"
            headers["Sec-Fetch-Mode"] = "cors"
            headers["Sec-Fetch-Dest"] = "empty"
        else:
            headers["Upgrade-Insecure-Requests"] = "1"
            headers["Sec-Fetch-Site"] = "none"
            headers["Sec-Fetch-Mode"] = "navigate"
            headers["Sec-Fetch-User"] = "?1"
            headers["Sec-Fetch-Dest"] = "document"
        # 不跟随重定向，让调用方处理 302
        return await self._client.request(method, url, headers=headers, data=data, follow_redirects=False)

    async def get_sms_login_page(self) -> tuple[str, str]:
        """获取 SMS 登录页，返回 (sessid_cookie, request_hash)

        流程：
        1. GET /auth/login?type=mobile 获取 HTML 和 SESSID cookie
        2. 从 HTML 解析 requestHash
        """
        resp = await self._auth_request("GET", "/auth/login?type=mobile")
        html = resp.text
        # 提取 SESSID
        sessid = ""
        for cookie in resp.headers.get_list("set-cookie"):
            if "SESSID=" in cookie:
                sessid = cookie.split(";")[0]
                break
        # 提取 requestHash（兼容新老页面格式，见 _extract_request_hash）
        request_hash = _extract_request_hash(html)
        if not request_hash:
            raise CoolapkError(
                "无法从登录页解析 requestHash（登录页可能又改版了，请到 "
                "https://github.com/yufeng-liang/coolapk-mcp-pro/issues 反馈）"
            )
        return sessid, request_hash

    async def get_captcha_image(self, sessid: str) -> bytes:
        """获取图形验证码图片 bytes

        Args:
            sessid: 从 get_sms_login_page 获取的 SESSID cookie
        """
        import time as _time

        ts = int(_time.time() * 1000)
        resp = await self._auth_request(
            "GET", f"/auth/showCaptchaImage?{ts}", sessid=sessid,
            referer="https://account.coolapk.com/auth/loginByCoolapk",
        )
        return resp.content

    async def send_sms_code(
        self, sessid: str, request_hash: str, phone: str, captcha: str, country: int = 86,
    ) -> dict:
        """发送短信验证码（POST /auth/login?type=mobile）

        这是 SMS 登录的第一步。服务端验证图形验证码后发送短信到手机。

        Args:
            sessid: SESSID cookie
            request_hash: 从登录页获取的 requestHash
            phone: 手机号
            captcha: 图形验证码（用户识别 get_captcha_image 返回的图片后输入）
            country: 国家码，默认 86

        Returns:
            API 响应 dict。
            - HTTP 302：验证码正确，短信已发送。Location 头是验证码输入页 URL。
              返回 {"status": 1, "validate_url": "<Location>", "message": "短信验证码已发送"}
            - HTTP 200 + JSON：验证码错误或频率限制。看 message 字段。
            - status==-1 表示需要图形验证码。
        """
        import time as _time

        data = {
            "submit": 1,
            "requestHash": request_hash,
            "country": country,
            "mobile": phone,
            "captcha": captcha,
            "randomNumber": f"{int(_time.time()*1000)}{int(_time.time()%1000)}",
            # 网易易盾反机器人 token：nscode=200 是固定成功码，
            # nstoken 用任意值即可通过服务端 APP 环境检测（实测验证）
            "nscode": "200",
            "nstoken": "coolapk_mcp_python_client",
        }
        resp = await self._auth_request("POST", "/auth/login?type=mobile", sessid=sessid, data=data)

        # 302 重定向 = 验证码正确，短信已发送
        if resp.status_code == 302:
            location = resp.headers.get("location", "")
            return {
                "status": 1,
                "message": "短信验证码已发送",
                "validate_url": location,
                "sessid": sessid,
            }

        # 200 JSON 响应
        try:
            result = resp.json()
        except Exception:
            text = resp.text[:500] if resp.text else ""
            raise CoolapkError(
                f"发送验证码响应解析失败: HTTP {resp.status_code}, body={text}"
            ) from None
        return result

    async def login_sms(self, sessid: str, validate_url: str, code: str) -> dict:
        """提交短信验证码完成登录

        这是 SMS 登录的第二步。send_sms_code 成功后返回的 validate_url 是
        `/auth/validateLogin?key=<token>` 格式。

        流程：
        1. GET validate_url 获取验证码输入页 HTML + 新的 requestHash
        2. POST validate_url 提交 code + type=code + requestHash

        Args:
            sessid: SESSID cookie（必须与 send_sms_code 用同一个 session）
            validate_url: send_sms_code 成功返回的 validate_url
            code: 用户收到的短信验证码

        Returns:
            API 响应 dict。成功后 Set-Cookie 里会有 uid/username/token。
        """
        import time as _time

        # validate_url 是路径格式 /auth/validateLogin?key=xxx
        path = validate_url
        if not validate_url.startswith("/"):
            # 整段 URL 输入时剥掉 host 部分
            for host in ("https://account.coolapk.com", "https://www.coolapk.com"):
                if validate_url.startswith(host):
                    path = validate_url[len(host):]
                    break
            else:
                path = f"/{validate_url}"

        # 1. GET validate 页面获取新的 requestHash
        resp = await self._auth_request("GET", path, sessid=sessid)
        html = resp.text
        request_hash = _extract_request_hash(html)

        # 2. POST 提交验证码
        data = {
            "submit": 1,
            "requestHash": request_hash,
            "code": code,
            "type": "code",
            "randomNumber": f"{int(_time.time()*1000)}{int(_time.time()%1000)}",
            # 网易易盾反机器人 token（同 send_sms_code）
            "nscode": "200",
            "nstoken": "coolapk_mcp_python_client",
        }
        resp = await self._auth_request(
            "POST", path, sessid=sessid, data=data,
            referer=f"https://account.coolapk.com{path}",
        )

        # 检查 302 重定向（登录成功）
        if resp.status_code == 302:
            location = resp.headers.get("location", "")
            cookies = {}
            for cookie in resp.headers.get_list("set-cookie"):
                for name in ("uid", "username", "token"):
                    if cookie.startswith(f"{name}="):
                        val = cookie.split(";")[0].split("=", 1)[1]
                        if val and val != "deleted":
                            cookies[name] = val
            return {
                "status": 1,
                "message": "登录成功",
                "redirect": location,
                "_cookies": cookies,
            }

        try:
            result = resp.json()
        except Exception:
            text = resp.text[:500] if resp.text else ""
            raise CoolapkError(
                f"短信验证码登录响应解析失败: HTTP {resp.status_code}, body={text}"
            ) from None

        # 从 Set-Cookie 提取 uid/username/token
        cookies = {}
        for cookie in resp.headers.get_list("set-cookie"):
            for name in ("uid", "username", "token"):
                if cookie.startswith(f"{name}="):
                    val = cookie.split(";")[0].split("=", 1)[1]
                    if val and val != "deleted":
                        cookies[name] = val
        result["_cookies"] = cookies
        return result

    def cache_stats(self) -> dict:
        """缓存统计（供 /status）：条数、字节、命中率、淘汰次数"""
        stats = self._cache.stats()
        stats["enabled"] = bool(throttle.CACHE_ENABLED)
        return stats

    def metrics(self) -> dict:
        """请求统计（供 /status）：累计请求/重试/缓存/单飞搭车与按 kind 的错误分布"""
        calls = self._metrics["calls"]
        total_ms = self._metrics["latency_ms_total"]
        return {
            "uptime_s": round(time.monotonic() - self._started_at, 1),
            "calls": calls,
            "latency_ms": {
                "total": round(total_ms, 1),
                "max": round(self._metrics["latency_ms_max"], 1),
                "avg": round(total_ms / calls, 1) if calls else 0.0,
            },
            "requests": self._metrics["requests"],
            "retries": self._metrics["retries"],
            "cache_hits": self._metrics["cache_hits"],
            "cache_misses": self._metrics["cache_misses"],
            "shared": self._metrics["shared"],
            "inflight": len(self._inflight),
            "errors": dict(self._metrics["errors"]),
        }

    async def close(self):
        # 退出前把待落盘的 uid 索引刷掉：合并窗口里的脏数据不能丢。
        # 先等后台任务跑完（它可能正在写盘，取消会让线程继续跑而结果丢失），
        # 再无条件补一次写——无条件是因为 dirty 标记可能已被后台任务清掉、
        # 而写盘却在取消竞态里没落地。
        # 先取消还没跑完的合并任务：它此刻多半还在 sleep 等窗口结束，
        # 等它等于让退出多停一整个窗口（默认 1s，测试里被调到 30s）。
        # 取消后由下面这次无条件写盘兜底，所以取消不会丢数据。
        task = self._index_flush_task
        self._index_flush_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - 合并任务异常不该阻断关闭
                logger.debug("uid 索引合并任务异常退出", exc_info=True)
        self._index_dirty = False
        try:
            await asyncio.to_thread(self._save_uid_index)
        except OSError:
            logger.warning("uid 索引退出前写盘失败", exc_info=True)
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()
