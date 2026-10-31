"""MCP Server — 酷安社区只读工具集（9 个工具）"""

import functools
import json
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


def main():
    mcp.run()


if __name__ == "__main__":
    main()
