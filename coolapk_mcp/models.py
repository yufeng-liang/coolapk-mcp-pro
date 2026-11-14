"""数据模型 — 精简字段，exclude_defaults 节省 token"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, field_validator, model_validator


_LINK_PATTERN = re.compile(
    r"https?://[^\s\"'<>]+|(?:(?:wwl\.|[a-z0-9-]+\.)?(?:pan\.quark|pan\.baidu|"
    r"aliyundrive|alipan|123pan|mega\.nz|cloud\.189|caiyun\.139|drive\.uc|"
    r"github\.com|gitee\.com|lanzou[a-z]*\.[a-z]+))[^\s\"'<>]*",
    re.IGNORECASE,
)


def _extract_links(text: str) -> list[str]:
    """从评论文本提取网盘/仓库链接（去重保序）。

    评论正文是 HTML，链接里的查询参数常被转义成 &amp;；先 html.unescape
    还原实体再剥尾部标点，保证 https://x/a?pwd=1&amp;y=2 不被截断成 ?pwd=1。
    """
    if not text:
        return []
    out: list[str] = []
    for m in _LINK_PATTERN.finditer(text):
        u = unescape(m.group(0)).rstrip(").,，。")
        if u and u not in out:
            out.append(u)
    return out


def _strip_html(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(separator=" ", strip=True)


# 酷安 dateline 是东八区 Unix 秒；固定 +08:00 偏移，不依赖运行环境本地时区
_CST = timezone(timedelta(hours=8))


def _format_time(timestamp: float | str) -> str:
    """Unix 秒 → 东八区 ISO8601（如 2023-11-15T06:13+08:00）。

    空值/非法值一律回退空串；数字字符串（接口偶尔返回）也按秒处理。
    """
    if not timestamp:
        return ""
    try:
        dt = datetime.fromtimestamp(int(timestamp), tz=_CST)
    except (ValueError, OSError, TypeError, OverflowError):
        return ""
    return dt.isoformat(timespec="minutes")


def _extract_pics(pic_arr: list[str] | str) -> list[str]:
    if isinstance(pic_arr, str):
        if not pic_arr:
            return []
        import json

        try:
            pic_arr = json.loads(pic_arr)
        except (json.JSONDecodeError, TypeError):
            return [str(pic_arr)] if pic_arr else []
    if isinstance(pic_arr, list):
        return [p for p in pic_arr if isinstance(p, str) and p]
    return []



def _ensure_string(v: Any) -> str:
    """Convert None and other falsy values to empty string."""
    if v is None:
        return ""
    return str(v) if not isinstance(v, str) else v


def _env_int(name: str, default: int) -> int:
    """读整数环境变量：非法值回落默认（与 client._env_int 同口径）。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError, OverflowError):
        # OverflowError：inf / 1e400 这类值能过 float() 却过不了 int()，此前会
        # 让 import 期直接崩，整个服务起不来
        return default


# 单条正文的输出上限（字符）。酷安长文可上万字，一次工具调用把几万字塞进模型
# 上下文既贵又会挤掉后续对话，而「被截断」如果不标出来，调用方会拿半截正文当
# 全文分析。超限时截断并置 content_truncated=True，需要全文走 share_url。
FEED_CONTENT_MAX_CHARS = _env_int("COOLAPK_FEED_CONTENT_MAX", 2000)
REPLY_CONTENT_MAX_CHARS = _env_int("COOLAPK_REPLY_CONTENT_MAX", 800)
# 单次输出的回复条数上限（all_replies 最多翻 30 页，一页 10 条即 300 条）。
REPLY_MAX_ITEMS = _env_int("COOLAPK_REPLY_MAX_ITEMS", 60)


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    """按字符数截断，返回 (文本, 是否截断)；limit<=0 表示不限制。"""
    if limit <= 0 or len(text) <= limit:
        return text, False
    return text[:limit].rstrip() + "…", True


class FeedModel(BaseModel):
    id: int = 0
    title: str = ""
    message: str = Field(exclude=True, default="")  # HTML，仅内部用
    content: str = ""  # 纯文本，由 model_validator 从 message 生成
    # 正文超过 FEED_CONTENT_MAX_CHARS 被截断（全文见 share_url / coolapk_get_url）
    content_truncated: bool = False
    feed_type: str = Field(alias="feedType", default="feed")
    tag: str = ""  # 话题标签（话题动态等列表条目会带，普通动态为空、exclude_defaults 不输出）
    username: str = ""
    uid: int = 0
    user_level: int = Field(default=0)
    # 热度三字段 0 值也输出：exclude_defaults 对"非默认值"生效的判断无法区分
    # 「0 赞」与「字段缺失」，而 AI 依赖热度判断结果质量与排序——改为 dump 前
    # 由 model_serializer 补回（见 convert_search_items 注释）
    like_num: int = Field(alias="likenum", default=0)
    reply_num: int = Field(alias="replynum", default=0)
    forward_num: int = Field(alias="forwardnum", default=0)
    dateline: str = ""
    device: str = Field(alias="deviceTitle", default="")
    ip_location: str = Field(alias="ipLocation", default="")
    images: list[str] = Field(default_factory=list)

    @field_validator(
        "title",
        "message",
        "username",
        "device",
        "ip_location",
        "feed_type",
        mode="before",
    )
    @classmethod
    def _ensure_string_fields(cls, v: Any) -> str:
        return _ensure_string(v)

    @model_validator(mode="after")
    def _generate_content(self) -> FeedModel:
        if not self.content and self.message:
            self.content = _strip_html(self.message)
        self.content, self.content_truncated = _truncate(
            self.content, FEED_CONTENT_MAX_CHARS
        )
        return self

    @field_validator("dateline", mode="before")
    @classmethod
    def _format_dateline(cls, v: Any) -> str:
        if isinstance(v, str) and v.lstrip("-").isdigit():
            v = int(v)
        if isinstance(v, (int, float)) and v:
            return _format_time(v)
        return str(v) if v else ""

    @classmethod
    def from_api(cls, data: dict) -> FeedModel:
        user_info = data.get("userInfo") or {}
        pics = data.get("picArr") or data.get("pic") or []
        return cls(
            id=data.get("id", 0),
            title=data.get("messageTitle", "") or data.get("title", ""),
            message=data.get("message", ""),
            feedType=data.get("feedType", "feed"),
            tag=data.get("tag", "") or "",
            username=user_info.get("username", ""),
            uid=user_info.get("uid", 0),
            user_level=user_info.get("level", 0),
            likenum=data.get("likenum", 0),
            replynum=data.get("replynum", 0),
            forwardnum=data.get("forwardnum", 0),
            dateline=data.get("dateline", 0),
            deviceTitle=data.get("deviceTitle", ""),
            ipLocation=data.get("ipLocation", ""),
            images=_extract_pics(pics),
        )


class FeedDetail(FeedModel):
    read_num: int = Field(alias="readNum", default=0)
    source_feed: FeedModel | None = None
    replies: list[ReplyModel] = Field(default_factory=list)
    reply_count: int = 0
    # 详情获取路径：api（详情接口）或 author_feed_fallback（风控回落，从作者动态列表读取）
    fetch_source: str = ""
    # 评论聚焦模式元信息（smart/author/hot）：楼主评论数、热评数、总评论数
    comments_focus: dict | None = None
    # 正文修改历史（作者每次编辑的快照，with_history=True 时填充）
    edit_history: list[dict] = Field(default_factory=list)
    # 深度展开楼中楼时补充的子回复条数（expand_sub_replies=True 时填充）
    sub_replies_expanded: int = 0
    # 楼中楼是否被截断：某父评论撞到页数上限/请求预算，还有子回复没取回
    sub_replies_truncated: bool = False
    # 全量评论翻页是否被截断（all_replies / comment_keyword 撞页数上限或预算）
    replies_truncated: bool = False
    # 全量评论续抓页码（未截断时为 None）
    replies_next_page: int | None = None
    # 因超过 REPLY_MAX_ITEMS 而未输出的回复条数（0 表示全部输出）
    replies_omitted: int = 0
    # 分享链接（带 s= 分享码，浏览器打开可看全文与图片，免服务器图片流量）
    share_url: str = ""

    @classmethod
    def from_api(
        cls, data: dict, replies: list[dict] | None = None,
        fetch_source: str = "", comments_focus: dict | None = None,
    ) -> FeedDetail:
        user_info = data.get("userInfo") or {}
        pics = data.get("picArr") or data.get("pic") or []
        source = data.get("forwardSourceFeed")
        source_feed = FeedModel.from_api(source) if source else None
        reply_rows = replies or []
        parsed_replies = [ReplyModel.from_api(r) for r in reply_rows]
        return cls(
            id=data.get("id", 0),
            title=data.get("messageTitle", "") or data.get("title", ""),
            message=data.get("message", ""),
            feedType=data.get("feedType", "feed"),
            username=user_info.get("username", ""),
            uid=user_info.get("uid", 0),
            user_level=user_info.get("level", 0),
            likenum=data.get("likenum", 0),
            replynum=data.get("replynum", 0),
            forwardnum=data.get("forwardnum", 0),
            dateline=data.get("dateline", 0),
            deviceTitle=data.get("deviceTitle", ""),
            ipLocation=data.get("ipLocation", ""),
            images=_extract_pics(pics),
            readNum=data.get("readNum", 0),
            source_feed=source_feed,
            replies=parsed_replies,
            reply_count=data.get("replyRowsCount", 0) or data.get("replynum", 0),
            fetch_source=fetch_source,
            share_url=data.get("shareUrl") or "",
            comments_focus=comments_focus,
        )


def cap_replies(detail: FeedDetail, limit: int | None = None) -> FeedDetail:
    """按 REPLY_MAX_ITEMS 限制输出的回复条数，并标记被省略的条数。

    为什么不是「截断就报错」：all_replies 一条帖子能翻 300 条，全塞进上下文会把
    后续对话挤掉，而只输出一部分却不说，调用方会把「前 60 条」当成全部评论。
    因此保留前 N 条 + replies_omitted 计数 + replies_truncated=True，让「还有
    多少没看」是可读的事实。
    """
    max_items = REPLY_MAX_ITEMS if limit is None else limit
    if max_items <= 0 or len(detail.replies) <= max_items:
        return detail
    detail.replies_omitted = len(detail.replies) - max_items
    detail.replies = detail.replies[:max_items]
    detail.replies_truncated = True
    return detail


class ReplyModel(BaseModel):
    id: int = 0
    message: str = Field(exclude=True, default="")
    content: str = ""
    # 回复正文超过 REPLY_CONTENT_MAX_CHARS 被截断（回复一般不长，阈值更低）
    content_truncated: bool = False
    username: str = ""
    uid: int = 0
    like_num: int = Field(alias="likenum", default=0)
    dateline: str = ""
    is_author: bool = Field(alias="isFeedAuthor", default=False)
    images: list[str] = Field(default_factory=list)
    # 楼中楼：子回复总数 + 内联子回复（服务端默认只带最近几条）
    reply_count: int = Field(alias="replynum", default=0)
    reply_to: str = Field(alias="rusername", default="")
    # 从评论文本提取的下载/仓库链接（server 层填充；工具帖资料多在评论链接里）
    links: list[str] = Field(default_factory=list)
    sub_replies: list["ReplyModel"] = Field(default_factory=list)

    @field_validator("message", "username", "reply_to", mode="before")
    @classmethod
    def _ensure_string_fields(cls, v: Any) -> str:
        return _ensure_string(v)

    @model_validator(mode="after")
    def _generate_content(self) -> ReplyModel:
        if not self.content and self.message:
            self.content = _strip_html(self.message)
        self.content, self.content_truncated = _truncate(
            self.content, REPLY_CONTENT_MAX_CHARS
        )
        return self

    @model_validator(mode="after")
    def _collect_links(self) -> ReplyModel:
        # 从原始 message（含超链接标签 href）提取网盘/仓库链接
        if not self.links:
            self.links = _extract_links(self.message or self.content)
        return self

    @field_validator("dateline", mode="before")
    @classmethod
    def _format_dateline(cls, v: Any) -> str:
        if isinstance(v, str) and v.lstrip("-").isdigit():
            v = int(v)
        if isinstance(v, (int, float)) and v:
            return _format_time(v)
        return str(v) if v else ""

    @classmethod
    def from_api(cls, data: dict) -> ReplyModel:
        user_info = data.get("userInfo") or {}
        pics = data.get("picArr") or data.get("pic") or []
        reply_rows = data.get("replyRows") or []
        return cls(
            id=data.get("id", 0),
            message=data.get("message", ""),
            username=user_info.get("username", ""),
            uid=user_info.get("uid", 0) or data.get("uid", 0),
            likenum=data.get("likenum", 0),
            dateline=data.get("dateline", 0),
            isFeedAuthor=bool(data.get("isFeedAuthor", 0)),
            images=_extract_pics(pics),
            replynum=data.get("replynum", 0),
            rusername=data.get("rusername", "") or "",
            sub_replies=[
                ReplyModel.from_api(sub)
                for sub in reply_rows
                if isinstance(sub, dict)
            ],
        )


class UserModel(BaseModel):
    uid: int = 0
    username: str = ""
    bio: str = ""
    level: int = 0
    fans_num: int = Field(alias="fans", default=0)
    follow_num: int = Field(alias="follow", default=0)
    feed_num: int = Field(alias="feed", default=0)
    verify_title: str = Field(alias="verifyTitle", default="")
    reg_date: str = Field(alias="regdate", default="")
    city: str = ""
    gender: str = ""

    @field_validator("username", "bio", "verify_title", "city", "gender", mode="before")
    @classmethod
    def _ensure_string_fields(cls, v: Any) -> str:
        return _ensure_string(v)

    @field_validator("reg_date", mode="before")
    @classmethod
    def _format_regdate(cls, v: Any) -> str:
        if isinstance(v, str) and v.lstrip("-").isdigit():
            v = int(v)
        if isinstance(v, (int, float)) and v:
            return _format_time(v)
        return str(v) if v else ""

    @field_validator("gender", mode="before")
    @classmethod
    def _format_gender(cls, v: Any) -> str:
        mapping = {1: "男", 2: "女"}
        if isinstance(v, int):
            return mapping.get(v, "")
        return str(v) if v else ""

    @classmethod
    def from_api(cls, data: dict) -> UserModel:
        return cls(
            uid=data.get("uid", 0),
            username=data.get("username", ""),
            bio=data.get("bio", ""),
            level=data.get("level", 0),
            fans=data.get("fans", 0),
            follow=data.get("follow", 0),
            feed=data.get("feed", 0),
            verifyTitle=data.get("verifyTitle", ""),
            regdate=data.get("regdate", 0),
            city=data.get("city", ""),
            gender=data.get("gender", 0),
        )


class TopicModel(BaseModel):
    id: int = 0
    title: str = ""
    tag: str = ""
    follow_num: int = Field(alias="follownum", default=0)
    comment_num: int = Field(alias="commentnum", default=0)
    description: str = ""

    @field_validator("title", "tag", "description", mode="before")
    @classmethod
    def _ensure_string_fields(cls, v: Any) -> str:
        return _ensure_string(v)

    @classmethod
    def from_api(cls, data: dict) -> TopicModel:
        return cls(
            id=data.get("id", 0),
            title=data.get("title", ""),
            tag=data.get("tag", "") or data.get("title", ""),
            follownum=data.get("follownum", 0) or data.get("follow_num", 0),
            commentnum=data.get("commentnum", 0)
            or data.get("newsnum", 0)
            or data.get("rating_total_num", 0),
            description=data.get("description", "")
            or data.get("newtitle", "")
            or data.get("username", ""),
        )


class AppModel(BaseModel):
    id: int = 0
    title: str = ""
    package_name: str = Field(alias="pkgname", default="")
    description: str = ""
    follow_num: int = Field(alias="followNum", default=0)
    download_num: int = Field(alias="downloadCount", default=0)
    version: str = Field(alias="versionName", default="")

    @field_validator("title", "package_name", "description", "version", mode="before")
    @classmethod
    def _ensure_string_fields(cls, v: Any) -> str:
        return _ensure_string(v)

    @classmethod
    def from_api(cls, data: dict) -> AppModel:
        return cls(
            id=data.get("id", 0),
            title=data.get("title", ""),
            pkgname=data.get("pkgname", ""),
            description=data.get("description", "") or data.get("keywords", ""),
            followNum=data.get("followNum", 0),
            downloadCount=data.get("downloadCount", 0) or data.get("downloadnum", 0),
            versionName=data.get("versionName", ""),
        )


# ---------------------------------------------------------------------------
# 工具输出白名单裁剪
# ---------------------------------------------------------------------------
# 各工具输出统一走 model_dump(exclude_defaults=True) + 白名单裁剪两道工序：
# 先由模型默认值剔除空字段，再按白名单保留 agent 实际需要的字段。
# 白名单外的字段（推广语、模板、分享链接等冗余）一律丢弃，而不是塞回输出。

# 动态/评论共用的核心字段：正文、作者、互动数、时间、图片、id
_FEED_FIELDS = (
    "id", "title", "content", "content_truncated", "tag", "feed_type", "username", "uid",
    "user_level", "like_num", "reply_num", "forward_num", "read_num",
    "dateline", "device", "ip_location", "images",
    # 质量派生字段（scoring 模块计算，服务端补进条目）
    "post_score", "discussion_ratio",
)
_REPLY_FIELDS = (
    "id", "content", "content_truncated", "username", "uid", "like_num", "dateline",
    "is_author", "images", "reply_count", "reply_to", "sub_replies",
    "comment_match", "links",
)

# 各工具的输出白名单：coolapk_search 按 entityType 分发，
# 其余键对应各工具名；feed_detail 的顶层与评论走同一套动态/评论字段
_TOOL_FIELDS: dict[str, tuple[str, ...]] = {
    "search_feed": _FEED_FIELDS,
    "search_user": ("uid", "username", "bio", "level", "fans_num", "follow_num",
                    "feed_num", "verify_title", "city", "gender"),
    "search_topic": ("id", "title", "tag", "follow_num", "comment_num", "description"),
    "search_app": ("id", "title", "package_name", "description", "follow_num",
                   "download_num", "version"),
    "feed_detail": (*_FEED_FIELDS, "share_url", "source_feed", "replies", "reply_count",
                    "fetch_source", "comment_search", "comments_focus",
                    "edit_history", "sub_replies_expanded",
                    "sub_replies_truncated", "replies_truncated",
                    "replies_next_page", "replies_omitted"),
    "user_profile": ("uid", "username", "bio", "level", "fans_num", "follow_num",
                     "feed_num", "verify_title", "reg_date", "city", "gender"),
    "user_feeds": _FEED_FIELDS,
    # 作者画像：UserModel 字段 + scoring 派生字段（author_tier 等）
    "author": ("uid", "username", "bio", "level", "fans_num", "follow_num",
               "feed_num", "verify_title", "reg_date", "city", "gender",
               "author_tier", "author_tier_label", "is_expert",
               "fans_per_feed", "expertise"),
    # 作者领域聚合：作者画像 + 过滤后的高赞帖列表
    "author_digest": ("uid", "username", "author", "author_error", "keyword", "keyword_mode",
                      "pages", "fetched", "matched", "truncated", "next_page", "posts"),

    "home": _FEED_FIELDS,
    "topic": ("id", "title", "tag", "follow_num", "comment_num", "description"),
    "topic_feeds": _FEED_FIELDS,
    # 板块 / 二手：与 home 同构的包装（feeds 走动态白名单，元字段单独保留）
    "board": _FEED_FIELDS,
    "secondhand": _FEED_FIELDS,
    # 应用详情：AppModel dump 后的字段名（camelCase 由 pydantic 字段名决定）
    # 键名对齐 AppModel.model_dump()（snake_case，未走 alias）
    "app_detail": ("id", "title", "package_name", "description",
                   "follow_num", "download_num", "version"),
    # 通知：各类型（@我的、评论、点赞、关注、私信）共用一套宽松键名，
    # 具体哪些键出现取决于通知类型，缺的就不输出
    "notification": ("id", "uid", "username", "message", "dateline", "title",
                     "content", "feed_id", "reply_id", "is_read", "pic", "ukey",
                     "entity_type", "url", "target_url"),
}
# get_url 透传响应里必须剔除的敏感字段（递归匹配，键名小写比较）；
# 键名含敏感词也算（accessToken/sessionId/userToken 等变体），config 可能
# 内嵌服务端配置同样剔除
_GET_URL_SENSITIVE_KEYS = frozenset(
    {"cookie", "cookies", "set-cookie", "token", "session", "sessid", "config"}
)
_GET_URL_SENSITIVE_SUBSTR = ("token", "session", "cookie", "sessid", "passwd", "password", "secret")

# user_feeds 新版包装结构的元数据字段（feeds 列表单独按动态白名单裁剪）
_USER_FEEDS_META = (
    "uid", "pages", "fetched", "matched", "sort", "keyword", "keyword_mode",
    "truncated", "next_page",
)
# home 包装结构的元字段（feeds 单独按动态白名单裁剪）
_HOME_META = ("tab", "tab_fallback", "page", "fetched", "has_more", "next_page")
# 板块 / 二手包装的元字段
_BOARD_META = ("board", "page", "fetched", "has_more", "next_page")
_SECONDHAND_META = ("brand", "page", "fetched", "has_more", "next_page")
# 通知包装的元字段（items 单独按通知白名单裁剪）
_NOTIFY_META = ("ntype", "page", "fetched", "counts", "has_more", "next_page")


def _apply_whitelist(obj: Any, fields: tuple[str, ...]) -> Any:
    """按白名单保留 dict 字段；list 逐项递归（裁剪后为空的占位条目丢弃）；
    其余类型原样返回。"""
    if isinstance(obj, dict):
        return {k: obj[k] for k in fields if k in obj}
    if isinstance(obj, list):
        pruned = [_apply_whitelist(v, fields) for v in obj]
        # 酷安列表常混广告位/占位条目，裁剪后成空 dict，直接丢弃避免输出 {} 噪声
        return [v for v in pruned if not (isinstance(v, dict) and not v)]
    return obj


def _prune_list(items: Any, fields: tuple[str, ...]) -> list[Any]:
    """按白名单裁剪动态/评论列表，丢弃裁剪后为空的占位条目（酷安列表
    常混广告位/空位条目，留着只会输出 {} 噪声）。"""
    if not isinstance(items, list):
        return items
    pruned = [_apply_whitelist(v, fields) for v in items]
    return [v for v in pruned if not (isinstance(v, dict) and not v)]


def apply_tool_whitelist(tool: str, data: Any) -> Any:
    """按工具白名单裁剪已 dump 的输出数据。

    - search 按条目内容分发到对应类型的白名单（suspected_ad/comment_match
      等标记字段始终保留）
    - feed_detail：顶层动态用动态白名单，replies 树用评论白名单递归裁剪
    - get_url：透传响应保留原始结构（键名是酷安原始 camelCase，不能按
      模型 snake_case 白名单裁剪，否则响应会被掏空），只递归剔除敏感字段
    - 未知工具名原样返回（不裁剪），避免误伤新增输出
    """
    if data is None:
        return data

    if tool == "search":
        return [_apply_search_whitelist(item) for item in data]

    if tool == "feed_detail":
        if isinstance(data, dict):
            out = _apply_whitelist(data, _TOOL_FIELDS["feed_detail"])
            if "replies" in data:
                out["replies"] = [
                    pruned for pruned in (_prune_reply(r) for r in data["replies"])
                    if not (isinstance(pruned, dict) and not pruned)
                ]
            return out
        return data

    if tool == "get_url":
        return _strip_sensitive(data)

    if tool == "user_feeds":
        # 新版返回 {"uid","pages","fetched","matched","sort","keyword","feeds":[...]}
        # 包装结构；纯 list 输出保持兼容
        if isinstance(data, dict) and "feeds" in data:
            out = {k: data[k] for k in _USER_FEEDS_META if k in data}
            out["feeds"] = _prune_list(data["feeds"], _TOOL_FIELDS["user_feeds"])
            return out
        return _prune_list(data, _TOOL_FIELDS["user_feeds"])

    if tool == "author":
        return _apply_whitelist(data, _TOOL_FIELDS["author"])

    if tool == "author_digest":
        if isinstance(data, dict):
            out = {
                k: data[k]
                for k in _TOOL_FIELDS["author_digest"]
                if k in data and k not in ("author", "posts")
            }
            if isinstance(data.get("author"), dict):
                out["author"] = _apply_whitelist(data["author"], _TOOL_FIELDS["author"])
            if "posts" in data:
                out["posts"] = _prune_list(data["posts"], _TOOL_FIELDS["user_feeds"])
            return out
        return data

    if tool == "home":
        # {"tab","page","fetched","feeds":[...]} 包装结构；纯 list 保持兼容
        if isinstance(data, dict) and "feeds" in data:
            out = {k: data[k] for k in _HOME_META if k in data}
            out["feeds"] = _prune_list(data["feeds"], _TOOL_FIELDS["home"])
            return out
        return _prune_list(data, _TOOL_FIELDS["home"])

    if tool == "board" or tool == "secondhand":
        meta = _BOARD_META if tool == "board" else _SECONDHAND_META
        if isinstance(data, dict) and "feeds" in data:
            out = {k: data[k] for k in meta if k in data}
            out["feeds"] = _prune_list(data["feeds"], _TOOL_FIELDS[tool])
            return out
        return _prune_list(data, _TOOL_FIELDS[tool])

    if tool == "app_detail":
        return _apply_whitelist(data, _TOOL_FIELDS["app_detail"])

    if tool == "notification":
        if isinstance(data, dict) and "items" in data:
            out = {k: data[k] for k in _NOTIFY_META if k in data}
            out["items"] = _prune_list(data["items"], _TOOL_FIELDS["notification"])
            return out
        return _prune_list(data, _TOOL_FIELDS["notification"])

    if tool == "topic_feeds":
        return _prune_list(data, _TOOL_FIELDS["topic_feeds"])

    if tool == "topic":
        # server 传入 {"topic": {...}, "feeds": [...], "topic_error"/"feeds_error"}：
        # 详情与动态各自可空，错误字段必须透传，否则 agent 看不出是「没有」还是「取不到」
        if isinstance(data, dict) and ("topic" in data or "feeds" in data or "topic_error" in data):
            wrapped: dict[str, Any] = {}
            for key in ("topic_error", "feeds_error"):
                if data.get(key):
                    wrapped[key] = data[key]
            topic = data.get("topic")
            if isinstance(topic, dict):
                wrapped["topic"] = _apply_whitelist(topic, _TOOL_FIELDS["topic"])
            feeds = data.get("feeds")
            if feeds is not None:
                wrapped["feeds"] = _prune_list(feeds, _TOOL_FIELDS["topic_feeds"])
            return wrapped
        out = _apply_whitelist(data, _TOOL_FIELDS["topic"])
        feeds = data.get("feeds") if isinstance(data, dict) else None
        if feeds is not None:
            out["feeds"] = _prune_list(feeds, _TOOL_FIELDS["topic_feeds"])
        return out

    if tool == "user_profile":
        # 空间接口可能在顶层或 firstItem 里带用户卡，两种都裁
        if isinstance(data, dict) and "uid" in data:
            return _apply_whitelist(data, _TOOL_FIELDS["user_profile"])
        if isinstance(data, dict):
            first = data.get("firstItem")
            if isinstance(first, dict) and "uid" in first:
                out = dict(data)
                out["firstItem"] = _apply_whitelist(first, _TOOL_FIELDS["user_profile"])
                return out
        return _apply_whitelist(data, _TOOL_FIELDS["user_profile"])

    # 未知工具名：原样返回
    return data


def _strip_sensitive(obj: Any) -> Any:
    """递归剔除敏感字段（get_url 透传响应用）。

    键名小写后整键匹配敏感名单；命中敏感子串（accessToken/sessionId 等
    变体键）也剔除。匹配到的键整个丢弃。list 逐项递归。
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            key = str(k).lower()
            if key in _GET_URL_SENSITIVE_KEYS or any(
                s in key for s in _GET_URL_SENSITIVE_SUBSTR
            ):
                continue
            out[k] = _strip_sensitive(v)
        return out
    if isinstance(obj, list):
        return [_strip_sensitive(v) for v in obj]
    return obj


def _apply_search_whitelist(item: dict) -> dict:
    """单条搜索结果按内容分发白名单；标记字段（suspected_ad、comment_match、
    comment_search）始终保留。feed 判定不能只看 like_num——互动数为 0 的
    动态会被 exclude_defaults 剔掉该字段，须结合 content/feed_type 判断。"""
    if not isinstance(item, dict):
        return item
    if "content" in item or "feed_type" in item:
        fields = _TOOL_FIELDS["search_feed"]
    elif "username" in item or "bio" in item:
        fields = _TOOL_FIELDS["search_user"]
    elif "tag" in item or "follow_num" in item:
        fields = _TOOL_FIELDS["search_topic"]
    elif "package_name" in item:
        fields = _TOOL_FIELDS["search_app"]
    else:
        fields = None
    pruned = _apply_whitelist(item, fields) if fields else dict(item)
    # 纯图帖等媒体动态 dump 后无 content/feed_type 键，会被上面的 elif 误套
    # user 白名单、只剩 uid/username——裁剪后丢字段过多时回落原条目兜底
    if fields and len(pruned) <= 2 and len(item) > 2:
        pruned = dict(item)
    for flag in ("suspected_ad", "comment_match", "comment_search"):
        if flag in item:
            pruned[flag] = item[flag]
    return pruned


def _prune_reply(reply: dict) -> dict:
    """裁剪单条评论及其楼中楼（sub_replies）为评论白名单。"""
    if not isinstance(reply, dict):
        return reply
    out = _apply_whitelist(reply, _REPLY_FIELDS)
    subs = reply.get("sub_replies")
    if subs:
        out["sub_replies"] = [
            pruned for pruned in (_prune_reply(s) for s in subs)
            if not (isinstance(pruned, dict) and not pruned)
        ]
    return out
