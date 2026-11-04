"""MCP Server — 酷安社区只读工具集（13 个工具）"""

import functools
import hashlib
import hmac
import json
import os
import time
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings

from coolapk_mcp import postfilter
from coolapk_mcp import scoring


from coolapk_mcp.client import CoolapkClient, CoolapkError
from coolapk_mcp.models import (
    FeedDetail,
    FeedModel,
    ReplyModel,
    TopicModel,
    UserModel,
    _strip_html as _strip_html_keep_links,
    apply_tool_whitelist,
)
from coolapk_mcp.config import AppConfig

# 图片中转只允许这些酷安域名，避免被当成任意 URL 的开放代理
IMAGE_HOSTS = frozenset({"image.coolapk.com", "avatar.coolapk.com", "static.coolapk.com"})
IMAGE_UA = (
    "Dalvik/2.1.0 (Linux; U; Android 16; 23113RKC6C Build/AQ3A.250226.002) "
    "(#Build; Redmi; 23113RKC6C; AQ3A.250226.002; HyperOS_3.0; 3.0.1.0) "
    "+CoolMarket/16.2.0-2604201-universal"
)

mcp = FastMCP(
    "coolapk",
    # 显式关闭 DNS rebinding 保护：FastMCP 在 host 为 127.0.0.1 时会自动开启，
    # 该校验按 Host 头白名单拦截，「Host: <公网IP>:端口」的合法直连（远程部署常态）
    # 会被误判 421；Bearer 鉴权已由 _build_http_app 强制，防护足够
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    instructions=(
        "酷安社区只读工具集：搜索帖子/用户/话题/应用、查看帖子详情与评论、"
        "用户资料与动态、话题详情、首页动态、登录状态、图片下载、通用只读接口透传、"
        "作者画像与作者深挖聚合。"
        "全部工具只读，不会发帖、点赞或关注。"
        "搜索默认启用严格关键词过滤与去重（strict=False 可关闭）；"
        "帖子详情在详情接口被风控时会自动回落到作者动态列表（fetch_source 字段标注来源）；"
        "评论搜索（comment_keyword）会自动拉全量评论并按关键词命中过滤。"
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


def _prepare_feeds(
    raw: Any, keyword: str = "", sort: str = "default"
) -> tuple[list[dict], int]:
    """把原始动态列表加工成带质量分的输出：过模型 → 关键词过滤 → 打分 → 排序。

    keyword 空格分隔多词，任一命中标题或正文即保留（与 search 的 strict 语义一致）。
    sort=hot 按 post_score（点赞+评论×3+转发×2）降序，latest 按发布时间降序。
    """
    if not isinstance(raw, list):
        return [], 0
    items = [
        _dump(FeedModel.from_api(f)) for f in raw if isinstance(f, dict)
    ]
    total = len(items)
    words = [w for w in (keyword or "").lower().split() if w]
    if words:
        items = [
            it
            for it in items
            if any(
                w in f"{it.get('title') or ''} {it.get('content') or ''}".lower()
                for w in words
            )
        ]
    for it in items:
        scoring.annotate_post(it)
    if sort == "hot":
        items.sort(key=lambda x: x.get("post_score") or 0, reverse=True)
    elif sort == "latest":
        items.sort(key=lambda x: str(x.get("dateline") or ""), reverse=True)
    return items, total


def _catch_api_error(fn):
    """把 CoolapkError 转成结构化 JSON（带 error_type 与 code），AI 客户端可据此
    程序化区分错误类别（如 403 登录失效 vs 1004 风控）。"""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        global _client
        try:
            return await fn(*args, **kwargs)
        except CoolapkError as e:
            if e.kind == "auth_expired":
                # 登录态失效：丢弃旧 client（其构造时快照的登录态已过期），
                # 下次调用重建并重新读 config——桌面端重登后直接重试即可生效
                _client = None
            return json.dumps(
                {"error": e.message, "error_type": e.kind, "code": e.error_code},
                ensure_ascii=False,
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
    client = _get_client()
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
    return json.dumps({"counts": meta, "results": results}, ensure_ascii=False)


@mcp.tool()
@_catch_api_error
async def coolapk_feed_detail(
    feed_id: int,
    with_replies: bool = True,
    reply_page: int = 1,
    all_replies: bool = False,
    comment_keyword: str | None = None,
    comments_focus: str = "default",
    with_history: bool = False,
    expand_sub_replies: bool = False,
) -> str:
    """获取酷安帖子的完整详情和回复（详情接口被风控时自动回落）。

    工具帖找资料建议用 comments_focus=smart：一次拿到「正文 + 楼主全部补充 +
    高赞反馈」，获取方式与答疑多在楼主回复里。

    Args:
        feed_id: 帖子ID
        with_replies: 是否包含回复列表，默认是
        reply_page: 回复页码（all_replies / comment_keyword / comments_focus 非 default 时忽略）
        all_replies: 拉全部一级评论（自动翻页，含内联楼中楼），适合完整舆情/评论分析
        comment_keyword: 按关键词检索评论和楼中楼（自动拉全量；空格分隔多词=任一命中；
            命中条目带 comment_match 字段，楼中楼按上下文保留，统计见 comment_search）
        comments_focus: 评论聚焦模式：default（默认时间序一页）/ smart（楼主全部回复
            + 热门评论，找工具帖资料首选）/ author（只看楼主）/ hot（只看热门）/
            all（全部一级评论，等同 all_replies）
        with_history: 附带正文修改历史（作者每次编辑的快照，看版本演进）
        expand_sub_replies: 深度展开楼中楼（对回复数多的父评论补全子回复，
            楼主答疑常在楼中楼深处；会额外发请求，只在需要时开）
    """
    client = _get_client()
    focus = (comments_focus or "default").strip().lower()
    if focus not in ("default", "smart", "author", "hot", "all"):
        focus = "default"

    # all_replies 与 focus=all 等价
    if focus == "all":
        all_replies = True

    smart = await client.get_feed_detail_smart(
        feed_id,
        with_replies=with_replies or bool(comment_keyword) or focus in ("smart", "author", "hot"),
        reply_page=reply_page,
        all_replies=all_replies or bool(comment_keyword),
    )
    detail = FeedDetail.from_api(
        smart["data"],
        smart["replies"] if isinstance(smart["replies"], list) else None,
        fetch_source=smart["source"],
    )

    # ---- 评论聚焦模式：单独拉楼主评论 / 热门评论替换默认列表 ----
    focus_meta: dict | None = None
    merged: list[dict] = []
    if focus in ("smart", "author", "hot"):
        try:
            author_list: list[dict] = []
            hot_list: list[dict] = []
            if focus in ("smart", "author"):
                author_list = await client.get_author_replies(feed_id)
            if focus in ("smart", "hot"):
                hot_list = await client.get_popular_replies(feed_id)
            seen: set[str] = set()
            for item in (author_list or []) + (hot_list or []):
                if isinstance(item, dict) and str(item.get("id")) not in seen:
                    seen.add(str(item.get("id")))
                    merged.append(item)
            focus_meta = {
                "mode": focus,
                "author_replies": len(author_list or []),
                "hot_replies": len(hot_list or []),
                "total_shown": len(merged),
                "total_replynum": (smart["data"].get("replynum") or 0),
            }
        except CoolapkError:
            focus_meta = {"mode": focus, "error": "评论聚焦拉取失败，已回落默认列表"}

    if focus_meta is not None:
        detail.comments_focus = focus_meta
        if merged:
            detail.replies = [ReplyModel.from_api(r) for r in merged]

    # ---- 修改历史 ----
    if with_history:
        try:
            hist = await client.get_feed_history(feed_id)
            if isinstance(hist, list) and hist:
                detail.edit_history = [
                    {
                        "id": h.get("id", 0),
                        "content": _strip_html_keep_links(h.get("message", "")),
                        "dateline": h.get("lastupdate", 0),
                    }
                    for h in hist
                    if isinstance(h, dict)
                ]
        except CoolapkError:
            detail.edit_history = []

    # ---- 楼中楼深度展开（楼主在楼中楼的答疑 + 高赞父评论） ----
    if expand_sub_replies and detail.replies:
        expanded = 0
        for i, r in enumerate(detail.replies[:12]):
            need = (r.reply_count or 0) - len(r.sub_replies or [])
            if need <= 0:
                continue
            try:
                subs = await client.get_sub_replies_paged(r.id, 1, max_pages=3)
            except CoolapkError:
                continue
            if subs:
                detail.replies[i].sub_replies = (
                    [ReplyModel.from_api(sr) for sr in subs]
                )
                expanded += len(subs)
        if expanded:
            detail.sub_replies_expanded = expanded

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
async def coolapk_user_feeds(
    uid: int,
    page: int = 1,
    pages: int = 1,
    keyword: str = "",
    sort: str = "default",
) -> str:
    """查看酷安用户发布的帖子（支持多页聚合、关键词过滤与热度排序）。

    Args:
        uid: 用户UID
        page: 起始页码（pages>1 时从该页开始连续抓取）
        pages: 连续抓取页数 1-5，默认 1；想看某作者的历史干货时设 2-3
        keyword: 只保留标题或正文含该词的帖子（空格分隔多词=任一命中）
        sort: 排序，default(接口原始顺序) / hot(互动热度) / latest(发布时间)
    """
    client = _get_client()
    start = max(1, int(page or 1))
    wanted = max(1, min(int(pages or 1), 5))
    if wanted > 1:
        raw = await client.get_user_feeds_pages(uid, pages=wanted, start_page=start)
    else:
        raw = await client.get_user_feeds(uid, start)
    if not isinstance(raw, list):
        return _json(raw)
    items, total = _prepare_feeds(raw, keyword, sort)
    return _json(
        {
            "uid": uid,
            "pages": wanted,
            "fetched": total,
            "matched": len(items),
            "sort": sort,
            "keyword": keyword or None,
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
    client = _get_client()
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
    uid: int, keyword: str = "", pages: int = 2, top: int = 10
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
    """
    client = _get_client()
    wanted = max(1, min(int(pages or 1), 5))
    raw = await client.get_user_feeds_pages(uid, pages=wanted)
    posts, total = _prepare_feeds(raw, keyword, "hot")
    limit = max(1, min(int(top or 10), 50))
    posts = posts[:limit]
    space = await client.get_user_space(uid)
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
            "keyword": keyword or None,
            "pages": wanted,
            "fetched": total,
            "matched": len(posts),
            "posts": posts,
        },
        whitelist="author_digest",
    )



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
async def coolapk_suggest(keyword: str) -> str:
    """获取酷安搜索联想词（查询改写积木）。

    输入自然语言或半成品关键词，返回酷安建议的规范搜索词，
    可用于把口语化需求改写成酷安搜索习惯用词后再搜。

    Args:
        keyword: 查询词（可以是口语化长句）
    """
    client = _get_client()
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
    secret = os.environ.get("COOLAPK_HTTP_TOKEN", "") or "coolapk-mcp-dev"
    try:
        ttl = max(1, min(int(ttl_minutes), 10080))
    except (TypeError, ValueError):
        ttl = 120
    exp = int(time.time()) + ttl * 60

    items: list[dict[str, Any]] = []
    _sep = chr(10)
    for raw in [part for part in urls.replace(_sep, " ").replace(",", " ").split() if part]:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https") or host not in IMAGE_HOSTS:
            items.append({"url": raw, "error": "只支持酷安图片域名（image/avatar/static.coolapk.com）"})
            continue
        sig = hmac.new(secret.encode(), f"{raw}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        items.append(
            {
                "url": raw,
                "link": f"{public_base}/img?u={quote(raw, safe='')}&exp={exp}&sig={sig}",
                "expires_at": exp,
            }
        )
    return json.dumps({"count": len(items), "ttl_minutes": ttl, "items": items}, ensure_ascii=False)


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
        )
    token = os.environ.get("COOLAPK_HTTP_TOKEN", "")

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
    if not is_loopback and not token:
        raise SystemExit(
            "[coolapk-mcp] 拒绝启动：COOLAPK_HTTP_HOST 绑定非回环地址（"
            f"{host}）但未设置 COOLAPK_HTTP_TOKEN。\n"
            "  HTTP 服务不设防 = 任何人都能用你的酷安登录态查数据。\n"
            "  设置 COOLAPK_HTTP_TOKEN=<随机长随机串> 后重试，"
            "公网部署请务必用 Nginx/Caddy 反代加 TLS（见 README 远程部署章）。"
        )

    # ---- 图片中转：把酷安 CDN 的防盗链图片转成可公开访问的签名链接 ----
    # 酷安 image.coolapk.com 对非客户端请求返回 HTTP 567，且多数聊天类客户端
    # （如小爱）拿不到 base64 就显示不了图，因此提供 /img 中转 + HMAC 签名。
    public_base = os.environ.get("COOLAPK_PUBLIC_BASE", "").rstrip("/")
    img_secret = token or "coolapk-mcp-dev"
    state = {"token": token, "public_base": public_base, "img_secret": img_secret}

    def _bearer_endpoint(inner_app):
        """Bearer Token 校验包裹 MCP 端点：/health 免鉴权，MCP 一律校验。"""

        async def _endpoint(scope, receive, send):
            if state["token"]:
                headers = dict(scope.get("headers") or [])
                auth = headers.get(b"authorization", b"").decode("latin-1")
                # RFC 9110：scheme 大小写不敏感；token 用恒定时间比较防时序侧信道
                scheme, _, cred = auth.partition(" ")
                expected = f"Bearer {state['token']}".encode()
                if scheme.lower() != "bearer" or not hmac.compare_digest(
                    cred.encode(), expected[len(b"Bearer "):]
                ):
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
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or (parsed.hostname or "").lower() not in IMAGE_HOSTS:
            return JSONResponse(
                {"error": "forbidden_host", "detail": "只允许转发酷安图片域名"}, status_code=403
            )
        if not hmac.compare_digest(sig, _img_sig(url, exp)):
            return JSONResponse(
                {"error": "bad_signature", "detail": "签名校验失败"}, status_code=403
            )
        # 缩略优先：带 @WxH 尺寸后缀的图先试酷安服务端缩略 .m.jpg（大图省约 40% 流量），
        # 上游 404/非 200 自动回落原图；COOLAPK_IMG_THUMBNAIL=0 关闭。
        fetch_url = url
        thumbnail_used = False
        want_thumb = os.environ.get("COOLAPK_IMG_THUMBNAIL", "1").lower() not in ("0", "false")
        if want_thumb and urlsplit(url).path.endswith(".jpg") and "@" in urlsplit(url).path:
            fetch_url = url + ".m.jpg"
            thumbnail_used = True
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True, trust_env=False) as client:
                upstream = await client.get(
                    fetch_url, headers={"User-Agent": IMAGE_UA, "Referer": "https://www.coolapk.com/"}
                )
                if thumbnail_used and upstream.status_code != 200:
                    upstream = await client.get(
                        url, headers={"User-Agent": IMAGE_UA, "Referer": "https://www.coolapk.com/"}
                    )
                    thumbnail_used = False
        except Exception as exc:  # 网络异常统一 502，不泄漏细节
            return JSONResponse(
                {"error": "fetch_failed", "detail": type(exc).__name__}, status_code=502
            )
        if upstream.status_code != 200:
            return JSONResponse(
                {"error": "upstream_status", "detail": f"上游返回 {upstream.status_code}"},
                status_code=502,
            )
        ctype = upstream.headers.get("content-type", "image/jpeg")
        if not ctype.startswith("image/"):
            ctype = "image/jpeg"
        return Response(
            upstream.content, media_type=ctype, headers={"Cache-Control": "public, max-age=86400"}
        )

    http_app = mcp.streamable_http_app()

    import contextlib

    @contextlib.asynccontextmanager
    async def _lifespan(app):
        # 等价于 streamable_http_app() 内置 lifespan：驱动 session_manager.run()
        async with mcp.session_manager.run():
            yield

    routes = [
        Route("/health", _health, methods=["GET"]),
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
