"""MCP Server — 酷安社区只读工具集（9 个工具）"""

import functools
import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP, Image

from coolapk_mcp import postfilter
from coolapk_mcp.client import CoolapkClient, CoolapkError
from coolapk_mcp.models import (
    FeedDetail,
    FeedModel,
    TopicModel,
    UserModel,
    apply_tool_whitelist,
)
from coolapk_mcp.config import AppConfig

mcp = FastMCP(
    "coolapk",
    instructions=(
        "酷安社区只读工具集：搜索帖子/用户/话题/应用、查看帖子详情与评论、"
        "用户资料与动态、话题详情、首页动态、登录状态、图片下载、通用只读接口透传。"
        "全部工具只读，不会发帖、点赞或关注。"
        "搜索默认启用严格关键词过滤与去重（strict=False 可关闭）；"
        "帖子详情在详情接口被风控时会自动回落到作者动态列表（fetch_source 字段标注来源）；"
        "评论搜索（comment_keyword）会自动拉全量评论并按关键词命中过滤。"
        "所有输出均经过白名单裁剪，只保留 agent 需要的核心字段；"
        "接口返回中的 cookie/token 等敏感字段一律剔除。"
    ),
)

_client: CoolapkClient | None = None


def _get_client() -> CoolapkClient:
    global _client
    if _client is None:
        _client = CoolapkClient()
    return _client


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


def _parse_feed_list(raw: Any) -> list[FeedModel]:
    if not isinstance(raw, list):
        return []
    return [FeedModel.from_api(f) for f in raw if isinstance(f, dict)]


def _catch_api_error(fn):
    """把 CoolapkError 转成结构化 JSON（带 error_type），AI 客户端可据此避免盲目重试"""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except CoolapkError as e:
            return json.dumps(
                {"error": e.message, "error_type": e.kind},
                ensure_ascii=False,
            )

    return wrapper


@mcp.tool()
@_catch_api_error
async def coolapk_search(
    keyword: str, type: str = "feed", page: int = 1, strict: bool = True
) -> str:
    """搜索酷安社区内容（自动去重 + 严格关键词过滤 + 疑似广告标记）。

    Args:
        keyword: 搜索关键词；多个词用空格分隔，strict 模式下要求全部命中
        type: 搜索类型，可选 feed(帖子) user(用户) feedTopic(话题) app(应用)
        page: 页码
        strict: 严格过滤（默认开）：归一化后每个关键词都必须出现在标题/正文/作者名里，
            可滤掉酷安搜索混入的不相关结果与机型名单广告；关闭则返回原始结果
    """
    client = _get_client()
    data = await client.search(keyword, type, page)
    if not isinstance(data, list):
        return _json(data)
    # feed 走严格过滤，user/feedTopic/app 仅剔除非实体卡片
    kept, meta = postfilter.filter_search_results(
        data, keyword, strict=strict if type == "feed" else False
    )
    results = postfilter.convert_search_items(kept)
    # results 已是 dump 后的 dict 列表，直接按条目类型分发白名单
    results = apply_tool_whitelist("search", results)
    return json.dumps({"counts": meta, "results": results}, ensure_ascii=False)


@mcp.tool()
@_catch_api_error
async def coolapk_feed_detail(
    feed_id: int,
    with_replies: bool = True,
    reply_page: int = 1,
    all_replies: bool = False,
    comment_keyword: str | None = None,
) -> str:
    """获取酷安帖子的完整详情和回复（详情接口被风控时自动回落）。

    Args:
        feed_id: 帖子ID
        with_replies: 是否包含回复列表，默认是
        reply_page: 回复页码（all_replies / comment_keyword 时忽略）
        all_replies: 拉全部一级评论（自动翻页，含内联楼中楼），适合完整舆情/评论分析
        comment_keyword: 按关键词检索评论和楼中楼（自动拉全量；空格分隔多词=任一命中；
            命中条目带 comment_match 字段，楼中楼按上下文保留，统计见 comment_search）
    """
    client = _get_client()
    smart = await client.get_feed_detail_smart(
        feed_id,
        with_replies=with_replies or bool(comment_keyword),
        reply_page=reply_page,
        all_replies=all_replies or bool(comment_keyword),
    )
    detail = FeedDetail.from_api(
        smart["data"],
        smart["replies"] if isinstance(smart["replies"], list) else None,
        fetch_source=smart["source"],
    )
    if comment_keyword:
        dumped = detail.model_dump(mode="json", exclude_defaults=True)
        dumped = postfilter.filter_comments(dumped, comment_keyword)
        return json.dumps(
            apply_tool_whitelist("feed_detail", dumped), ensure_ascii=False
        )
    return _json(detail, whitelist="feed_detail")


@mcp.tool()
@_catch_api_error
async def coolapk_user_profile(uid: int) -> str:
    """查看酷安用户资料。

    Args:
        uid: 用户UID
    """
    client = _get_client()
    data = await client.get_user_space(uid)
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        return _json(UserModel.from_api(data), whitelist="user_profile")
    return _json(data)


@mcp.tool()
@_catch_api_error
async def coolapk_user_feeds(uid: int, page: int = 1) -> str:
    """查看酷安用户发布的帖子。

    Args:
        uid: 用户UID
        page: 页码
    """
    client = _get_client()
    data = await client.get_user_feeds(uid, page)
    if isinstance(data, list):
        return _json(_parse_feed_list(data), whitelist="user_feeds")
    return _json(data)


@mcp.tool()
@_catch_api_error
async def coolapk_home(tab: str = "recommend", page: int = 1) -> str:
    """获取酷安首页动态。

    Args:
        tab: 类型，可选 recommend(推荐) hot(热门) latest(最新)
        page: 页码
    """
    client = _get_client()
    if tab == "hot":
        data = await client.get_hot_feeds(page)
    elif tab == "latest":
        data = await client.get_latest_feeds(page)
    else:
        data = await client.get_home_feeds(page)
    if isinstance(data, list):
        return _json(_parse_feed_list(data), whitelist="home")
    return _json(data)


@mcp.tool()
@_catch_api_error
async def coolapk_topic(tag: str, with_feeds: bool = False, page: int = 1) -> str:
    """查看酷安话题详情或话题下的帖子。

    Args:
        tag: 话题标签名
        with_feeds: 是否获取话题下的帖子列表
        page: 帖子页码
    """
    client = _get_client()
    result: dict[str, Any] = {}
    detail = await client.get_topic_detail(tag)
    if isinstance(detail, list) and detail:
        detail = detail[0]
    if isinstance(detail, dict):
        result["topic"] = TopicModel.from_api(detail)

    if with_feeds:
        feeds = await client.get_topic_feeds(tag, page)
        if isinstance(feeds, list):
            result["feeds"] = _parse_feed_list(feeds)
    return _json(result, whitelist="topic")


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


@mcp.tool()
@_catch_api_error
async def coolapk_read_image(url: str) -> Image:
    """下载酷安 CDN 图片并作为图片返回（用于查看动态配图、头像等）。

    仅允许酷安图片域名：image.coolapk.com / avatar.coolapk.com /
    static.coolapk.com。失败时返回结构化错误，可改用 coolapk_get_url
    获取接口数据，或直接向用户呈现 URL。

    Args:
        url: 图片完整 URL（http 自动升级 https）
    """
    client = _get_client()
    data, fmt = await client.download_image(url)
    return Image(data=data, format=fmt)


@mcp.tool()
@_catch_api_error
async def coolapk_get_url(path: str) -> str:
    """只读透传：GET 任意酷安 /v6/ 只读接口路径并返回裁剪后的 JSON。

    仅允许 api.coolapk.com 的 /v6/ 开头路径；含写动作的端点
    （点赞/回复/关注/发帖/私信等）一律 403 拒绝。响应保留原始
    结构（键名为酷安原始 camelCase），仅剔除 cookie/token 等敏感字段。
    适合获取列表页未覆盖的数据（应用详情、板块、二级评论等）。

    Args:
        path: 接口路径或完整 URL，如 /v6/apk/detail?id=com.tencent.mm
    """
    client = _get_client()
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


def _build_http_app():
    """构造带 Bearer Token 鉴权的 streamable-http ASGI 应用。"""
    import ipaddress
    import socket

    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    host = os.environ.get("COOLAPK_HTTP_HOST", "127.0.0.1")
    port = int(os.environ.get("COOLAPK_HTTP_PORT", "8000"))
    token = os.environ.get("COOLAPK_HTTP_TOKEN", "")

    # 非回环地址必须显式配 token，防止登录态裸奔公网
    try:
        addr = ipaddress.ip_address(socket.gethostbyname(host))
        is_loopback = addr.is_loopback
    except (socket.gaierror, ValueError):
        is_loopback = False  # 解析不出就按公网对待，从严
    if not is_loopback and not token:
        raise SystemExit(
            "[coolapk-mcp] 拒绝启动：COOLAPK_HTTP_HOST 绑定非回环地址（"
            f"{host}）但未设置 COOLAPK_HTTP_TOKEN。\n"
            "  HTTP 服务不设防 = 任何人都能用你的酷安登录态查数据。\n"
            "  设置 COOLAPK_HTTP_TOKEN=<随机长随机串> 后重试，"
            "公网部署请务必用 Nginx/Caddy 反代加 TLS（见 README 远程部署章）。"
        )

    state = {"token": token}

    def _bearer_endpoint(inner_app):
        """Bearer Token 校验包裹 MCP 端点：/health 免鉴权，MCP 一律校验。"""

        async def _endpoint(scope, receive, send):
            if state["token"]:
                headers = dict(scope.get("headers") or [])
                auth = headers.get(b"authorization", b"").decode("latin-1")
                if auth != f"Bearer {state['token']}":
                    resp = JSONResponse(
                        {"error": "unauthorized", "detail": "缺少或错误的 Bearer Token"},
                        status_code=401,
                        headers={"WWW-Authenticate": "Bearer"},
                    )
                    await resp(scope, receive, send)
                    return
            await inner_app(scope, receive, send)

        return _endpoint

    def _health(request):
        # 不暴露任何状态细节，只证明服务活着
        return JSONResponse({"status": "ok"})

    http_app = mcp.streamable_http_app()

    import contextlib

    @contextlib.asynccontextmanager
    async def _lifespan(app):
        # 等价于 streamable_http_app() 内置 lifespan：驱动 session_manager.run()
        async with mcp.session_manager.run():
            yield

    routes = [
        Route("/health", _health, methods=["GET"]),
        Mount(
            "/",
            app=_bearer_endpoint(http_app),
        ),
    ]
    # Bearer 校验包住整个 streamable-http 子应用（原生 /mcp 端点原样可达，
    # 含 GET/POST/DELETE 与 GET /mcp 建流），/health 在子应用外免鉴权。
    return Starlette(routes=routes, lifespan=_lifespan), host, port


def main():
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
