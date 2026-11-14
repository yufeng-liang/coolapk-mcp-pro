"""CLI 入口 — 精简 JSON 输出，节省 token"""

import asyncio
import json
import os
import sys
import tempfile
from functools import wraps
from pathlib import Path
from urllib.parse import unquote, urlsplit

import click

from coolapk_mcp import scoring
from coolapk_mcp.boards import BOARD_URLS
from coolapk_mcp.budget import Budget, BudgetExceeded
from coolapk_mcp.client import CoolapkClient, CoolapkError
from coolapk_mcp.config import AppConfig, has_valid_sessid, parse_cookie_str
from coolapk_mcp.linkparse import parse_feed_id
from coolapk_mcp.logging_utils import setup_logging
from coolapk_mcp.models import (
    _format_time,
    _strip_html,
    AppModel,
    FeedDetail,
    FeedModel,
    ReplyModel,
    TopicModel,
    UserModel,
    apply_tool_whitelist,
    cap_replies,
    REPLY_MAX_ITEMS,
)


def _dump(obj):
    """模型 → dict，排除默认值"""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", exclude_defaults=True)
    return obj


def _output(data):
    """统一 JSON 输出，紧凑格式节省 token"""
    if data is None:
        click.echo(json.dumps({"error": "无数据"}, ensure_ascii=False))
        sys.exit(1)
    if isinstance(data, list):
        data = [_dump(item) for item in data]
    else:
        data = _dump(data)
    click.echo(json.dumps(data, ensure_ascii=False))


def _output_error(err: CoolapkError):
    """错误输出（error_type 供调用方区分 captcha/网络/业务错误，避免盲目重试）"""
    payload = {"error": err.message, "code": err.error_code, "error_type": err.kind}
    # 与服务端同口径：等多久、能不能原样重试，CLI 用户同样需要
    if err.retry_after is not None:
        payload["retry_after"] = round(float(err.retry_after), 1)
    if err.retryable:
        payload["retryable"] = True
    click.echo(json.dumps(payload, ensure_ascii=False))
    sys.exit(1)


def async_command(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            asyncio.run(f(*args, **kwargs))
        except CoolapkError as e:
            _output_error(e)
        except KeyboardInterrupt:
            sys.exit(130)
    return wrapper


def _parse_feeds(raw) -> list[FeedModel]:
    """从 dataList 原始响应解析 feed 列表，自动过滤 card 条目"""
    if not isinstance(raw, list):
        return []
    feeds = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if item.get("entityType") == "feed":
            feeds.append(FeedModel.from_api(item))
        # card 内嵌的 entities 也可能是 feed
        elif item.get("entityType") == "card":
            for sub in item.get("entities") or []:
                if isinstance(sub, dict) and sub.get("entityType") == "feed":
                    feeds.append(FeedModel.from_api(sub))
    return feeds


def _ensure_utf8_stdio() -> None:
    """把 stdout/stderr 切到 UTF-8：Windows 控制台默认 cp1252/cp936 时中文会直接崩。

    为什么放在入口而不是 cli() 里：click 的 --help 在 parse_args 阶段就打印，那时
    群组回调还没执行，改编码已经晚了（CI 的 windows 腿上 `coolapk --help` 就是
    这么报 UnicodeEncodeError: 'charmap' codec can't encode characters 的）。
    errors=replace 是兜底：控制台字体画不出某个字符时输出替换符，也比整体崩掉好。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue


def main() -> None:
    """console_scripts 入口：先修编码再交给 click 解析参数。"""
    _ensure_utf8_stdio()
    cli()


@click.group()
def cli():
    """酷安社区 CLI — AI 原生搜索工具"""
    setup_logging()


# 页大小在 import 期取一次：CLI 的测试会把 CoolapkClient 换掉，运行时再取类属性
# 会炸在测试桩上；与 models 的 import 期常量同口径
_PAGE_SIZE = CoolapkClient.PAGE_SIZE


def _paging_meta(page: int, filled: int) -> dict:
    """列表命令的分页提示：与服务端 coolapk_home 同口径（本页填满即可能还有下一页）。

    filled 必须传接口返回的原始条目数，不能传解析后的动态数（卡片会被滤掉、
    卡片内嵌套动态会被展开，两个方向都会让末页判定失真）。
    """
    size = max(1, int(_PAGE_SIZE))
    has_more = filled >= size
    return {"has_more": has_more, "next_page": page + 1 if has_more else None}


# ---- 浏览 ----


@cli.command("home")
@click.option("--tab", type=click.Choice(
    ["recommend", "follow", "dynamic", "关注", "hot", "latest"]), default="recommend")
@click.option("--page", default=1, type=int)
@async_command
async def home(tab, page):
    """首页动态（recommend 走算法推荐，follow 只看已关注的人）"""
    page = max(1, int(page or 1))
    async with CoolapkClient() as client:
        if tab in ("follow", "dynamic", "关注"):
            tab = "follow"
            raw = await client.get_home_feeds(page)
        elif tab == "hot":
            raw = await client.get_hot_feeds(page)
        elif tab == "latest":
            raw = await client.get_latest_feeds(page)
        else:
            tab = "recommend"
            raw = await client.get_main_feeds(page)
    feeds = _parse_feeds(raw)
    # 与服务端 coolapk_home 同口径包装：只输出 feeds 列表时，调用方不知道该不该
    # 翻下一页（列表被卡片滤短后尤其判断不出来）
    _output(
        {
            "tab": tab,
            "page": page,
            "fetched": len(feeds),
            # _output 只对「顶层是 list」递归转 dict，嵌在包装里的模型要自己转
            "feeds": [_dump(item) for item in feeds],
            **_paging_meta(page, len(raw) if isinstance(raw, list) else 0),
        }
    )


@cli.command("main")
@click.option("--page", default=1, type=int)
@async_command
async def main_tab(page):
    """首页推荐（main/indexV8）"""
    async with CoolapkClient() as client:
        raw = await client.get_main_feeds(page)
    _output(_parse_feeds(raw))


@cli.command("hot")
@click.option("--page", default=1, type=int)
@async_command
async def hot(page):
    """热榜"""
    async with CoolapkClient() as client:
        raw = await client.get_board_feeds("hot", page)
    _output(_parse_feeds(raw))


@cli.command("boards")
def boards():
    """列出所有板块"""
    result = [
        {"id": bid, "title": title, "url": url}
        for bid, (title, url) in BOARD_URLS.items()
    ]
    _output(result)


@cli.command("board")
@click.argument("board_id")
@click.option("--page", default=1, type=int)
@async_command
async def board(board_id, page):
    """浏览板块（board_id 可用板块ID/page_name/url）"""
    async with CoolapkClient() as client:
        raw = await client.get_board_feeds(board_id, page)
    _output(_parse_feeds(raw))


@cli.command("secondhand")
@click.option("--brand", type=int, default=None, help="品牌ID（1005=苹果）")
@click.option("--page", default=1, type=int)
@async_command
async def secondhand(brand, page):
    """二手交易"""
    async with CoolapkClient() as client:
        raw = await client.get_secondhand_feeds(brand, page)
    _output(_parse_feeds(raw))


@cli.command("feed")
@click.argument("feed_id", type=str)
@click.option("--replies/--no-replies", default=True)
@click.option("--reply-page", default=1, type=int)
@click.option("--feed-type", default="feed",
              type=click.Choice(["feed", "trade", "feedArticle"]))
@click.option("--uid", "author_uid", type=int, default=None,
              help="作者UID（详情接口被风控时用于回落，不提供则自动查本地索引）")
@click.option("--all-replies", is_flag=True,
              help="拉全部一级评论（自动翻页，含内联楼中楼）")
@click.option("--grep", "comment_keyword", default=None,
              help="按关键词检索评论/楼中楼（隐含 --all-replies；空格分隔多词=任一命中）")
@click.option("--focus", "comments_focus", default="default",
              type=click.Choice(["default", "smart", "author", "hot", "all"]),
              help="评论聚焦：smart(楼主回复+热门)/author(仅楼主)/hot(仅热门)")
@click.option("--focus-page", default=1, type=int,
              help="焦点评论起始页（续抓上一次的 focus_meta.next_page）")
@click.option("--history", "with_history", is_flag=True, help="附带正文修改历史")
@click.option("--expand-sub-replies", is_flag=True,
              help="深度展开楼中楼（每条父评论最多补 3 页，有预算上限）")
@async_command
async def feed(feed_id, replies, reply_page, feed_type, author_uid, all_replies,
               comment_keyword, comments_focus, focus_page, with_history,
               expand_sub_replies):
    """动态详情（详情接口被验证码拦截时自动从作者动态列表回落）

    FEED_ID 可以是纯数字，也可以直接粘酷安分享链接/整段分享文案。
    """
    from coolapk_mcp.focus import collect_focus

    resolved_id = parse_feed_id(feed_id)
    if resolved_id is None:
        _output_error(
            CoolapkError(
                "无法从 FEED_ID 解析出帖子 ID：传数字，或粘贴酷安分享链接/分享文案",
                kind="api",
            )
        )
    feed_id = resolved_id
    focus = comments_focus
    if focus == "all":
        all_replies = True
    focus_active = focus in ("smart", "author", "hot")
    want_replies = replies or bool(comment_keyword)
    async with CoolapkClient() as client:
        smart = await client.get_feed_detail_smart(
            feed_id, author_uid=author_uid,
            # 与服务端同源：focus 活跃时不预拉默认回复（collect_focus 会整份替换
            # replies），真取不到时在下面回拉一页兜底，常规路径少一次限速窗口
            with_replies=want_replies and not focus_active,
            reply_page=reply_page,
            all_replies=all_replies or bool(comment_keyword),
        )
        detail = FeedDetail.from_api(
            smart["data"],
            smart["replies"] if isinstance(smart["replies"], list) else None,
            fetch_source=smart["source"],
        )
        raw_replies = smart.get("replies")
        if raw_replies is not None:
            detail.replies_truncated = bool(getattr(raw_replies, "truncated", False))
            detail.replies_next_page = getattr(raw_replies, "next_page", None)
        if focus_active:
            merged, meta = await collect_focus(
                client, feed_id, focus, start_page=max(1, int(focus_page or 1))
            )
            if meta is not None:
                meta["total_replynum"] = smart["data"].get("replynum") or 0
                detail.comments_focus = meta
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
                    detail.comments_focus = {
                        **(detail.comments_focus or {}),
                        "fallback": "default_replies",
                    }
        if with_history:
            try:
                hist = await client.get_feed_history(feed_id)
            except CoolapkError:
                hist = []
            if isinstance(hist, list):
                detail.edit_history = [
                    {
                        "id": h.get("id", 0),
                        "content": _strip_html(h.get("message", "")),
                        "dateline": _format_time(h.get("lastupdate", 0)),
                    }
                    for h in hist
                    if isinstance(h, dict)
                ]
        if expand_sub_replies and detail.replies:
            budget = Budget(max_requests=24, deadline=20.0)
            for i, r in enumerate(detail.replies[:12]):
                need = (r.reply_count or 0) - len(r.sub_replies or [])
                if need <= 0:
                    continue
                try:
                    subs = await client.get_sub_replies_paged(r.id, 1, max_pages=3, budget=budget)
                except (BudgetExceeded, CoolapkError):
                    break
                if getattr(subs, "truncated", False):
                    detail.sub_replies_truncated = True
                if subs:
                    detail.replies[i].sub_replies = [ReplyModel.from_api(sr) for sr in subs]
                    detail.sub_replies_expanded = (detail.sub_replies_expanded or 0) + len(subs)
    if comment_keyword:
        from coolapk_mcp import postfilter

        # 与服务端同源：检索在条数截断之前做，否则命中落在第 61 条之后会被
        # 报成「没有」（用户问的正是「有没有人提到 X」，这是假阴性）
        data = _dump(detail)
        data = postfilter.filter_comments(data, comment_keyword)
        matched = data.get("replies") or []
        if REPLY_MAX_ITEMS > 0 and len(matched) > REPLY_MAX_ITEMS:
            data["replies"] = matched[:REPLY_MAX_ITEMS]
            data["replies_omitted"] = len(matched) - REPLY_MAX_ITEMS
            data["replies_truncated"] = True
        if data.get("replies_truncated"):
            # 与服务端同源：不标出来会被误读成「关键词只命中这些」
            stats = data.setdefault("comment_search", {})
            stats["truncated"] = True
            if detail.replies_next_page is not None:
                stats["next_page"] = detail.replies_next_page
    else:
        # 与服务端同口径：回复条数上限，超出的部分用 replies_omitted 标出来
        data = _dump(cap_replies(detail))
    _output(data)


# ---- 搜索 ----


@cli.command("search")
@click.argument("keyword")
@click.option("--type", "search_type", default="feed",
              type=click.Choice(["feed", "user", "feedTopic", "app"]))
@click.option("--page", default=1, type=int)
@click.option("--strict/--no-strict", default=True,
              help="严格关键词过滤（默认开）：归一化后每个关键词都需命中标题/正文/作者名")
@click.option("--meta", is_flag=True, help="输出过滤统计信息（默认只输出结果列表）")
@click.option("--sort", default="hot",
              type=click.Choice(["hot", "reply", "default"]),
              help="帖子排序（hot 高赞优先，推荐）")
@async_command
async def search(keyword, search_type, page, strict, meta, sort):
    """搜索内容（自动去重 + 严格过滤 + 疑似广告标记）"""
    from coolapk_mcp import postfilter

    async with CoolapkClient() as client:
        raw = await client.search(keyword, search_type, page, sort=sort)
    if not isinstance(raw, list):
        _output(raw)
        return
    use_strict = strict and search_type == "feed"
    kept, counts = postfilter.filter_search_results(raw, keyword, strict=use_strict)
    results = postfilter.convert_search_items(kept)
    if meta:
        _output({"counts": counts, "results": results})
    else:
        _output(results)


# ---- 用户 ----


@cli.command("user")
@click.argument("uid", type=int)
@click.option("--feeds", is_flag=True, help="查看用户动态")
@click.option("--page", default=1, type=int)
@async_command
async def user(uid, feeds, page):
    """查看用户"""
    async with CoolapkClient() as client:
        if feeds:
            raw = await client.get_user_feeds(uid, page)
            _output(_parse_feeds(raw))
        else:
            raw = await client.get_user_space(uid)
            if isinstance(raw, list) and raw:
                raw = raw[0]
            _output(UserModel.from_api(raw) if isinstance(raw, dict) else raw)


# ---- 话题 ----


@cli.command("topic")
@click.argument("tag")
@click.option("--feeds", is_flag=True, help="查看话题下的帖子")
@click.option("--page", default=1, type=int)
@async_command
async def topic(tag, feeds, page):
    """话题详情"""
    async with CoolapkClient() as client:
        if feeds:
            raw = await client.get_topic_feeds(tag, page)
            _output(_parse_feeds(raw))
        else:
            raw = await client.get_topic_detail(tag)
            if isinstance(raw, list) and raw:
                raw = raw[0]
            _output(TopicModel.from_api(raw) if isinstance(raw, dict) else raw)


# ---- 通知 ----


@cli.command("notify")
@click.argument("ntype", default="checkCount",
                type=click.Choice(["checkCount", "atMeMeFeed", "atComment", "comment", "feedLike", "contactsFollow", "message"]))
@click.option("--page", default=1, type=int)
@click.option("--with-count", "with_count", is_flag=True, help="附带给未读数（多一次请求；checkCount 本身即未读数）")
@async_command
async def notify(ntype, page, with_count):
    """查看通知（默认只输出列表本体，--with-count 时包成 {items, counts}）"""
    async with CoolapkClient() as client:
        if ntype == "checkCount":
            _output(await client.get_notification_count())
            return
        data = await client.get_notifications(ntype, page)
        if with_count:
            try:
                counts = await client.get_notification_count()
            except CoolapkError as exc:
                counts = {"error": exc.message}
            _output({"items": data, "counts": counts})
            return
    _output(data)


# ---- 应用 / 作者 / 联想 / 透传 / 图片外链 ----


@cli.command("app-detail")
@click.argument("package_name")
@async_command
async def app_detail(package_name):
    """应用详情（按包名，如 com.coolapk.market）"""
    # 与服务端同口径：空包名直接给可读错误，不发请求（服务端只会回「未知的查询参数」）
    if not package_name.strip():
        _output_error(CoolapkError("package_name 不能为空", error_code=-1, kind="api"))
    async with CoolapkClient() as client:
        raw = await client.get_app_detail(package_name)
    if isinstance(raw, list) and raw:
        raw = raw[0]
    _output(AppModel.from_api(raw) if isinstance(raw, dict) else raw)


@cli.command("author")
@click.argument("uid", type=int)
@click.option("--top", default=10, type=int, help="附带评分最高的 N 条动态（0=不带，上限 50）")
@click.option("--pages", default=2, type=int, help="抓取页数（1-5）；作者的高分动态常在第 2/3 页")
@async_command
async def author(uid, top, pages):
    """作者画像：等级/粉丝/专长推断 + 高分动态

    与 MCP 工具 coolapk_author_digest 同口径：多页抓取并按 post_score 排序，
    返回 pages/fetched/truncated/next_page 元数据，避免把「最新一页」当「全部」。
    """
    limit = max(0, min(top, 50))
    wanted = max(1, min(pages, 5))
    async with CoolapkClient() as client:
        raw = await client.get_user_space(uid)
        if isinstance(raw, list) and raw:
            raw = raw[0]
        profile = UserModel.from_api(raw) if isinstance(raw, dict) else None
        data = profile.model_dump(mode="json", exclude_defaults=True) if profile else {}
        posts: list[dict] = []
        meta: dict = {}
        if data:
            budget = Budget(max_requests=wanted + 2, deadline=20.0)
            result = await client.get_user_feeds_pages(uid, pages=wanted, budget=budget)
            for item in _parse_feeds(list(result)):
                entry = item.model_dump(mode="json", exclude_defaults=True)
                # annotate_post 同时补 post_score 与 discussion_ratio，字段与服务端一致
                scoring.annotate_post(entry)
                posts.append(entry)
            posts.sort(key=lambda p: p["post_score"], reverse=True)
            meta = {**result.meta(), "pages": wanted, "fetched": len(posts)}
        if not data:
            _output(None)
        # 专长推断看正文关键词，取前 5 条足够；与 --top 无关，--top 0 也要留着
        data["posts_texts"] = [str(p.get("content") or "")[:600] for p in posts[:5]]
        data.update(scoring.summarize_author(data))
        data.update(meta)
        data["top_posts"] = posts[:limit]
    _output(data)


@cli.command("suggest")
@click.argument("keyword")
@async_command
async def suggest(keyword):
    """搜索联想词（把口语化需求改写成酷安习惯用词）"""
    async with CoolapkClient() as client:
        data = await client.get_topic_suggest(keyword)
    words: list[str] = []
    for card in data if isinstance(data, list) else []:
        if not isinstance(card, dict):
            continue
        word = str(card.get("title") or "").strip()
        # 联想卡是 title + searchTab:// 链接，没有 searchWord；剔除「搜索用户：」类前缀卡
        if word and not word.startswith("搜索"):
            words.append(word)
    _output({"suggests": words[:8]})


@cli.command("get-url")
@click.argument("path")
@async_command
async def get_url(path):
    """只读透传任意 /v6/ 接口（严格白名单默认开启）"""
    async with CoolapkClient() as client:
        data = await client.get_url(path)
    # 与 MCP 工具同口径：先剔敏感键再输出，别把 token / cookie 打到终端
    _output(apply_tool_whitelist("get_url", data))


@cli.command("image")
@click.argument("url")
@click.option("-o", "--output", "output_path", default="", help="保存路径（默认写到当前目录，文件名取自 URL）")
@async_command
async def image(url, output_path):
    """下载酷安图片到本地（MCP 侧对应 coolapk_read_image）"""
    async with CoolapkClient() as client:
        data, fmt = await client.download_image(url)
    default_name = urlsplit(url).path.rsplit("/", 1)[-1] or f"coolapk-image.{fmt}"
    target = Path(output_path).expanduser() if output_path else Path.cwd() / default_name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    _output({"saved": str(target), "bytes": len(data), "format": fmt})


@cli.command("image-link")
@click.argument("urls", nargs=-1, required=True)
@click.option("--ttl", "ttl_minutes", default=120, type=int,
              help="有效期分钟（1-10080，超出自动夹取；非数字直接报错，服务端则回落默认 120）")
@async_command
async def image_link(urls, ttl_minutes):
    """把酷安图片转成 /img 签名外链（需 COOLAPK_PUBLIC_BASE）"""
    # 延迟导入：只有这个命令需要签名密钥与 FastMCP 所在的 server 模块
    from coolapk_mcp.img_link import build_image_links
    from coolapk_mcp.server import _resolve_img_secret

    public_base = os.environ.get("COOLAPK_PUBLIC_BASE", "").rstrip("/")
    if not public_base:
        click.echo(
            json.dumps(
                {
                    "error": "未配置 COOLAPK_PUBLIC_BASE",
                    "hint": "远程 HTTP 部署时设为对外地址，例如 https://mcp.example.com",
                },
                ensure_ascii=False,
            )
        )
        sys.exit(1)
    secret = _resolve_img_secret(os.environ.get("COOLAPK_HTTP_TOKEN", "").strip())
    _output(build_image_links(" ".join(urls), public_base, secret, ttl_minutes))

# ---- 登录 ----


WEB_LOGIN_URL = "https://account.coolapk.com/auth/login?type=coolapk&forward=https://www.coolapk.com/"
WEB_LOGIN_TIMEOUT = 300  # 秒，超时未登录成功按失败处理


def _validate_login_cookies(cookies: dict[str, str]) -> str | None:
    """校验登录 Cookie 必备键；合法返回 None，否则返回错误信息"""
    uid = str(cookies.get("uid", "") or "").strip()
    if not uid or uid in ("0", "10000"):
        return f"Cookie 缺少有效 uid（当前 uid={uid or '空'}，需非空且非 0/10000）"
    if not str(cookies.get("token", "") or "").strip():
        return "Cookie 缺少 token"
    return None


def _apply_cookie_login(config: AppConfig, cookie_str: str) -> dict:
    """解析校验 Cookie 串并写入 config 持久化；失败返回含 error 的 dict"""
    cookies = parse_cookie_str(cookie_str)
    err = _validate_login_cookies(cookies)
    if err:
        return {"error": err}
    config.uid = cookies["uid"].strip()
    config.username = unquote(cookies.get("username", ""))
    config.token = cookies["token"].strip()
    config.desktop_cookie = cookie_str
    config.save()
    return {
        "status": "ok",
        "uid": config.uid,
        "username": config.username,
        "sessid_ok": has_valid_sessid(cookie_str),
    }


def _run_pywebview_login() -> str | None:
    """pywebview 打开酷安网页登录，轮询 Cookie，成功返回完整 Cookie 串

    轮询中把各次读到的 Cookie 累积进 jar：登录成功瞬间服务域在
    account.coolapk.com，随后重定向到 www.coolapk.com 可能只暴露部分键，
    累积可跨导航时机抓全 SESSID/uid/username/token。
    """
    import time

    import webview

    result: dict = {"cookie_str": "", "closed": False}

    def poll(window):
        jar: dict[str, str] = {}
        deadline = time.time() + WEB_LOGIN_TIMEOUT
        error_count = 0
        while time.time() < deadline and not result["closed"]:
            try:
                for sc in window.get_cookies() or []:
                    for key, morsel in sc.items():
                        value = getattr(morsel, "value", None)
                        if value:
                            jar[key] = value
                error_count = 0
            except Exception as exc:
                # 连续失败按异常计数，超限提前终止而不是让用户干等整个超时窗口
                error_count += 1
                if error_count >= 20:
                    click.echo(
                        json.dumps({"error": f"读取登录窗口 Cookie 连续失败: {exc}"},
                                   ensure_ascii=False),
                        err=True,
                    )
                    break
            else:
                error_count = 0
            sessid = jar.get("SESSID", "")
            if (
                sessid
                and sessid.lower() not in ("deleted", "expired")
                and jar.get("uid")
                and jar.get("token")
            ):
                result["cookie_str"] = "; ".join(f"{k}={v}" for k, v in jar.items())
                break
            time.sleep(0.5)
        try:
            window.destroy()
        except Exception:
            pass

    window = webview.create_window(
        "酷安登录 — 登录成功后窗口会自动关闭",
        WEB_LOGIN_URL,
        width=480,
        height=760,
    )
    if window is None:
        raise RuntimeError("创建登录窗口失败（pywebview 未初始化）")
    window.events.closed += lambda _w=None: result.update(closed=True)
    webview.start(poll, (window,))
    return result["cookie_str"] or None


def _run_web_login() -> tuple[str | None, dict | None]:
    """网页登录：优先系统浏览器 + CDP，找不到浏览器再回退 pywebview

    系统浏览器这条路不依赖 pythonnet，因此 Python 3.14 也能用；pywebview 在
    Windows 上的后端依赖 pythonnet，而 pythonnet 尚未支持 3.14。
    """
    try:
        from coolapk_mcp.browser_login import run_browser_login

        cookie_str, status = run_browser_login(WEB_LOGIN_URL, WEB_LOGIN_TIMEOUT)
    except Exception as exc:
        click.echo(
            json.dumps({"hint": f"系统浏览器登录不可用，尝试 pywebview: {exc}"}, ensure_ascii=False),
            err=True,
        )
        cookie_str, status = None, "no_browser"

    if status == "ok" and cookie_str:
        return cookie_str, None
    if status in ("timeout", "closed"):
        # 浏览器已经打开过，登录没完成说明是用户侧没走完流程，换引擎也没用
        return None, None
    if status == "launch_failed":
        return None, {
            "error": "浏览器无法启动（可能受限环境或缺少图形界面）",
            "hint": (
                "三条替代方案：①把 COOLAPK_BROWSER 指向 chrome/edge 可执行文件后重试；"
                "②自己用带 --remote-debugging-port=9222 的参数启动 Chrome，"
                "再设 COOLAPK_CDP_PORT=9222 运行本命令，登录后会自动读取；"
                "③手动抓 Cookie 后用 coolapk login --cookie"
            ),
        }

    # status == "no_browser"：本机没有 Chrome/Edge，回退 pywebview
    return _run_pywebview_login(), None


@cli.command("login")
@click.option("--web", is_flag=True, help="打开系统浏览器网页登录（Chrome/Edge + CDP 读 Cookie，Python 3.14 也可用）")
@click.option("--cookie", help="导入完整 Cookie 串（uid=..;username=..;token=..[;SESSID=..]）")
@click.option("--status", is_flag=True, help="查看登录状态")
@click.option("--adb", is_flag=True, help="从已连接的手机 ADB 自动提取 Cookie 登录")
@click.option("--sms", is_flag=True, help="短信验证码登录")
@click.option("--phone", help="手机号（SMS 登录用）")
@click.option("--captcha", help="图形验证码（SMS 发送验证码时需要）")
@click.option("--code", help="短信验证码（SMS 登录第二步）")
@click.option("--sessid", help="SESSID cookie（SMS 登录第二步，从第一步响应获取）")
@click.option("--validate-url", help="验证 URL（SMS 登录第二步，从第一步响应的 message 获取）")
@click.option("--captcha-file", default=None, help="图形验证码图片保存路径（默认系统临时目录）")
@async_command
async def login(web, cookie, status, adb, sms, phone, captcha, code, sessid, validate_url, captcha_file):
    """登录管理

    登录方式（四选一）：
      coolapk login --web            浏览器窗口网页登录（推荐）
      coolapk login --sms ...        短信验证码登录（两步流程见下）
      coolapk login --cookie "<串>"  直接导入 Cookie 串
      coolapk login --adb            从已连接手机提取 Cookie

    SMS 登录两步流程：
    第一步（发送验证码）：
      coolapk login --sms --phone <手机号> --captcha <图形验证码>
      → 不带 --captcha 时先把验证码图片存到临时目录，人工识别后带 --captcha 重跑
      → 成功后返回 sessid 和 validate_url（message 字段）

    第二步（提交短信验证码）：
      coolapk login --sms --code <短信验证码> --sessid <第一步的sessid> --validate-url <第一步的message>
      → 成功后自动写入登录态（完整 Cookie 串含 SESSID）
    """
    config = AppConfig.load()
    captcha_file = captcha_file or str(Path(tempfile.gettempdir()) / "coolapk_captcha.jpg")

    # --status: 查看登录状态
    if status:
        _output({
            "logged_in": config.is_logged_in,
            "uid": config.uid,
            "username": config.username,
            "source": config.source,
            "login_mode": config.login_mode,
            # 与 MCP 工具 coolapk_login_status 同口径
            "device_code": bool(config.device_code),
            "sessid_ok": has_valid_sessid(config.desktop_cookie) if config.desktop_cookie else None,
        })
        return

    # --web: 网页登录
    if web:
        try:
            cookie_str, web_error = _run_web_login()
        except Exception as e:
            click.echo(json.dumps(
                {"error": f"网页登录窗口启动失败: {e}", "hint": "无图形环境时请改用 --sms 或 --cookie"},
                ensure_ascii=False,
            ))
            sys.exit(1)
        if not cookie_str:
            click.echo(json.dumps(
                web_error
                or {"error": "网页登录未完成（窗口关闭或超时），未获取到登录 Cookie"},
                ensure_ascii=False,
            ))
            sys.exit(1)
        result = _apply_cookie_login(config, cookie_str)
        if "error" in result:
            click.echo(json.dumps(result, ensure_ascii=False))
            sys.exit(1)
        result["source"] = "web"
        _output(result)
        return

    # --adb: 从手机提取 Cookie
    if adb:
        cookie_str = _extract_cookie_from_adb()
        if not cookie_str:
            _output({"error": "未能从手机提取 Cookie，请确认手机已连接且酷安已登录"})
            sys.exit(1)
        result = _apply_cookie_login(config, cookie_str)
        if "error" in result:
            click.echo(json.dumps(result, ensure_ascii=False))
            sys.exit(1)
        _output(result)
        return

    # --sms: SMS 登录
    if sms:
        # 第二步：提交短信验证码
        if code and sessid and validate_url:
            async with CoolapkClient() as client:
                data = await client.login_sms(sessid, validate_url, code)
            cookies = data.get("_cookies", {}) if isinstance(data, dict) else {}
            if cookies.get("uid") and cookies.get("token"):
                # 三键拼上 SESSID 存完整串，后续请求可整串直用
                full_cookie = "; ".join(
                    [sessid] + [f"{k}={v}" for k, v in cookies.items()]
                )
                result = _apply_cookie_login(config, full_cookie)
                if "error" in result:
                    click.echo(json.dumps(result, ensure_ascii=False))
                    sys.exit(1)
                result["message"] = data.get("message", "")
                _output(result)
            else:
                # 失败响应可能带 _cookies（含 token），剥离后再回显
                safe = {k: v for k, v in data.items() if k != "_cookies"} if isinstance(data, dict) else data
                _output(safe)
            return

        # 第一步：发送验证码
        if phone and captcha:
            async with CoolapkClient() as client:
                if sessid:
                    # 复用之前的 session（验证码图片已在该 session 获取）
                    request_hash_to_use = validate_url or ""
                    data = await client.send_sms_code(sessid, request_hash_to_use, phone, captcha)
                else:
                    # 新 session：获取登录页 SESSID + requestHash
                    sid, request_hash = await client.get_sms_login_page()
                    data = await client.send_sms_code(sid, request_hash, phone, captcha)
                    sessid = sid
            if isinstance(data, dict):
                data.setdefault("sessid", sessid)
            _output(data)
            return

        # 只传 phone 没传 captcha：获取验证码图片
        if phone and not captcha:
            async with CoolapkClient() as client:
                sid, request_hash = await client.get_sms_login_page()
                captcha_bytes = await client.get_captcha_image(sid)
            Path(captcha_file).write_bytes(captcha_bytes)
            _output({
                "step": "captcha_required",
                "sessid": sid,
                "request_hash": request_hash,
                "captcha_file": captcha_file,
                "next": f"请识别 {captcha_file} 中的验证码，然后运行："
                        f" coolapk login --sms --phone {phone} --captcha <验证码> --sessid '{sid}' --validate-url '{request_hash}'",
            })
            return

        _output({"error": "SMS 登录需要 --phone 参数。用法见 --help"})
        sys.exit(1)

    # --cookie: 手动 Cookie 登录
    if cookie:
        result = _apply_cookie_login(config, cookie)
        if "error" in result:
            click.echo(json.dumps(result, ensure_ascii=False))
            sys.exit(1)
        _output(result)
        return

    click.echo(json.dumps(
        {"error": "请使用 --web / --cookie / --adb / --sms 登录，或 --status 查看状态"},
        ensure_ascii=False,
    ))
    sys.exit(1)


def _extract_cookie_from_adb() -> str | None:
    """从已连接的手机 ADB 提取酷安 Cookie

    前提：手机已 root，酷安已登录。
    从 /data/data/com.coolapk.market/shared_prefs/coolapk_preferences_v7.xml 提取
    uid/username/token。
    """
    import subprocess
    import re

    # adb server 冷启动/手机唤醒可能超过 10s，超时过短会偶发提取失败；
    # 给足 30s 并重试一次（第二次跑 adb server 已就绪，基本必成）
    last_error: Exception | None = None
    for _ in range(2):
        try:
            result = subprocess.run(
                ["adb", "shell", "su", "-c",
                 "cat /data/data/com.coolapk.market/shared_prefs/coolapk_preferences_v7.xml"],
                capture_output=True, text=True, timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            last_error = exc
            continue

        xml = result.stdout
        if xml:
            break
    else:
        if last_error is not None:
            # 别静默返回 None：adb 缺失 / 手机没连 / 未 root 三种最常见的失败
            # 都只会走到这里，用户拿不到任何线索
            click.echo(
                f"ADB 提取失败：{type(last_error).__name__}: {last_error}\n"
                "请确认：① adb 在 PATH 中；② 手机已连接并授权 USB 调试；③ 酷安已登录且手机已 root",
                err=True,
            )
        return None

    if not xml:
        return None

    uid = re.search(r'<string name="uid">([^<]+)</string>', xml)
    username = re.search(r'<string name="username">([^<]+)</string>', xml)
    token = re.search(r'<string name="token">([^<]+)</string>', xml)

    if not (uid and username and token):
        return None

    return f"uid={uid.group(1)};username={username.group(1)};token={token.group(1)}"


if __name__ == "__main__":
    main()
