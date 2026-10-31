"""数据模型 — 精简字段，exclude_defaults 节省 token"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, field_validator, model_validator


def _strip_html(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(separator=" ", strip=True)


def _format_time(timestamp: int | float) -> str:
    if not timestamp:
        return ""
    try:
        dt = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        return ""


def _extract_pics(pic_arr: list[str] | str) -> list[str]:
    if isinstance(pic_arr, str):
        if not pic_arr:
            return []
        import json

        try:
            pic_arr = json.loads(pic_arr)
        except (json.JSONDecodeError, TypeError):
            return [pic_arr] if pic_arr else []
    if isinstance(pic_arr, list):
        return [p for p in pic_arr if isinstance(p, str) and p]
    return []



def _ensure_string(v: Any) -> str:
    """Convert None and other falsy values to empty string."""
    if v is None:
        return ""
    return str(v) if not isinstance(v, str) else v


class FeedModel(BaseModel):
    id: int = 0
    title: str = ""
    message: str = Field(exclude=True, default="")  # HTML，仅内部用
    content: str = ""  # 纯文本，由 model_validator 从 message 生成
    feed_type: str = Field(alias="feedType", default="feed")
    tag: str = ""  # 话题标签（话题动态等列表条目会带，普通动态为空、exclude_defaults 不输出）
    username: str = ""
    uid: int = 0
    user_level: int = Field(default=0)
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

    @classmethod
    def from_api(
        cls, data: dict, replies: list[dict] | None = None,
        fetch_source: str = "",
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
        )


class ReplyModel(BaseModel):
    id: int = 0
    message: str = Field(exclude=True, default="")
    content: str = ""
    username: str = ""
    uid: int = 0
    like_num: int = Field(alias="likenum", default=0)
    dateline: str = ""
    is_author: bool = Field(alias="isFeedAuthor", default=False)
    images: list[str] = Field(default_factory=list)
    # 楼中楼：子回复总数 + 内联子回复（服务端默认只带最近几条）
    reply_count: int = Field(alias="replynum", default=0)
    reply_to: str = Field(alias="rusername", default="")
    sub_replies: list["ReplyModel"] = Field(default_factory=list)

    @field_validator("message", "username", "reply_to", mode="before")
    @classmethod
    def _ensure_string_fields(cls, v: Any) -> str:
        return _ensure_string(v)

    @model_validator(mode="after")
    def _generate_content(self) -> ReplyModel:
        if not self.content and self.message:
            self.content = _strip_html(self.message)
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


class MessageModel(BaseModel):
    """私信消息模型"""
    id: int = 0
    uid: int = 0
    username: str = ""
    ukey: str = ""
    message: str = ""
    dateline: str = ""
    is_me: bool = Field(alias="isMe", default=False)
    pic: str = ""

    @field_validator("username", "message", "ukey", "pic", mode="before")
    @classmethod
    def _ensure_string_fields(cls, v: Any) -> str:
        return _ensure_string(v)

    @field_validator("dateline", mode="before")
    @classmethod
    def _format_dateline(cls, v: Any) -> str:
        if isinstance(v, str) and v.lstrip("-").isdigit():
            v = int(v)
        if isinstance(v, (int, float)) and v:
            return _format_time(v)
        return str(v) if v else ""

    @classmethod
    def from_api(cls, data: dict) -> MessageModel:
        user_info = data.get("userInfo") or {}
        return cls(
            id=data.get("id", 0),
            uid=data.get("uid", 0) or user_info.get("uid", 0),
            username=user_info.get("username", "") or data.get("username", ""),
            ukey=data.get("ukey", ""),
            message=data.get("message", ""),
            dateline=data.get("dateline", 0),
            isMe=bool(data.get("isMe", 0)),
            pic=data.get("messagePic", "") or data.get("pic", ""),
        )


class ChatSessionModel(BaseModel):
    """私信会话列表条目"""
    uid: int = 0
    username: str = ""
    ukey: str = ""
    last_message: str = Field(alias="lastMessage", default="")
    dateline: str = ""
    unread: int = 0
    avatar: str = ""

    @field_validator("username", "ukey", "last_message", "avatar", mode="before")
    @classmethod
    def _ensure_string_fields(cls, v: Any) -> str:
        return _ensure_string(v)

    @field_validator("dateline", mode="before")
    @classmethod
    def _format_dateline(cls, v: Any) -> str:
        if isinstance(v, str) and v.lstrip("-").isdigit():
            v = int(v)
        if isinstance(v, (int, float)) and v:
            return _format_time(v)
        return str(v) if v else ""

    @classmethod
    def from_api(cls, data: dict) -> ChatSessionModel:
        user_info = data.get("userInfo") or {}
        return cls(
            uid=data.get("uid", 0) or user_info.get("uid", 0),
            username=user_info.get("username", "") or data.get("username", ""),
            ukey=data.get("ukey", ""),
            lastMessage=data.get("lastMessage", "") or data.get("message", ""),
            dateline=data.get("dateline", 0) or data.get("lastDateline", 0),
            unread=data.get("unreadNum", 0) or data.get("unread", 0),
            avatar=user_info.get("userSmallAvatar", "") or data.get("avatar", ""),
        )


# ---------------------------------------------------------------------------
# 工具输出白名单裁剪
# ---------------------------------------------------------------------------
# 各工具输出统一走 model_dump(exclude_defaults=True) + 白名单裁剪两道工序：
# 先由模型默认值剔除空字段，再按白名单保留 agent 实际需要的字段。
# 白名单外的字段（推广语、模板、分享链接等冗余）一律丢弃，而不是塞回输出。

# 动态/评论共用的核心字段：正文、作者、互动数、时间、图片、id
_FEED_FIELDS = (
    "id", "title", "content", "tag", "feed_type", "username", "uid",
    "user_level", "like_num", "reply_num", "forward_num", "read_num",
    "dateline", "device", "ip_location", "images",
)
_REPLY_FIELDS = (
    "id", "content", "username", "uid", "like_num", "dateline",
    "is_author", "images", "reply_count", "reply_to", "sub_replies",
    "comment_match",
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
    "feed_detail": (*_FEED_FIELDS, "source_feed", "replies", "reply_count",
                    "fetch_source", "comment_search"),
    "user_profile": ("uid", "username", "bio", "level", "fans", "follow", "feed",
                     "verify_title", "reg_date", "city", "gender"),
    "user_feeds": _FEED_FIELDS,
    "home": _FEED_FIELDS,
    "topic": ("id", "title", "tag", "follow_num", "comment_num", "description"),
    "topic_feeds": _FEED_FIELDS,
}
# get_url 透传响应里必须剔除的敏感字段（递归匹配，键名小写比较）；
# 键名含敏感词也算（accessToken/sessionId/userToken 等变体），config 可能
# 内嵌服务端配置同样剔除
_GET_URL_SENSITIVE_KEYS = frozenset(
    {"cookie", "cookies", "set-cookie", "token", "session", "sessid", "config"}
)
_GET_URL_SENSITIVE_SUBSTR = ("token", "session", "cookie", "sessid", "passwd", "secret")


def _apply_whitelist(obj: Any, fields: tuple[str, ...]) -> Any:
    """按白名单保留 dict 字段；list 逐项递归；其余类型原样返回。"""
    if isinstance(obj, dict):
        return {k: obj[k] for k in fields if k in obj}
    if isinstance(obj, list):
        return [_apply_whitelist(v, fields) for v in obj]
    return obj


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
                out["replies"] = [_prune_reply(r) for r in data["replies"]]
            return out
        return data

    if tool == "get_url":
        return _strip_sensitive(data)

    if tool == "user_feeds":
        return [_apply_whitelist(v, _TOOL_FIELDS["user_feeds"]) for v in data]

    if tool == "home":
        return [_apply_whitelist(v, _TOOL_FIELDS["home"]) for v in data]

    if tool == "topic_feeds":
        return [_apply_whitelist(v, _TOOL_FIELDS["topic_feeds"]) for v in data]

    if tool == "topic":
        # server 传入 {"topic": {...}, "feeds": [...]} 包装结构，分别裁剪
        if isinstance(data, dict) and ("topic" in data or "feeds" in data):
            out: dict[str, Any] = {}
            topic = data.get("topic")
            if isinstance(topic, dict):
                out["topic"] = _apply_whitelist(topic, _TOOL_FIELDS["topic"])
            feeds = data.get("feeds")
            if feeds is not None:
                out["feeds"] = [
                    _apply_whitelist(v, _TOOL_FIELDS["topic_feeds"]) for v in feeds
                ]
            return out
        out = _apply_whitelist(data, _TOOL_FIELDS["topic"])
        feeds = data.get("feeds") if isinstance(data, dict) else None
        if feeds is not None:
            out["feeds"] = [
                _apply_whitelist(v, _TOOL_FIELDS["topic_feeds"]) for v in feeds
            ]
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
        out["sub_replies"] = [_prune_reply(s) for s in subs]
    return out
