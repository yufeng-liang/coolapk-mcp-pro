"""models.apply_tool_whitelist 回归测试：输出裁剪、敏感字段剔除与兜底。

裁剪层的职责是「把模型 dump 出来的冗余字段挡在 agent 视野外」，因此这里的断言
都盯具体字段集合：既验证白名单内的字段留下，也验证白名单外的字段真的没了。
"""

from __future__ import annotations

from coolapk_mcp.models import apply_tool_whitelist


# ---------------------------------------------------------------------------
# home / topic：包装结构裁剪，元字段保留、错误字段透传
# ---------------------------------------------------------------------------


def test_home_wrapper_keeps_meta_and_prunes_feeds() -> None:
    data = {
        "tab": "hot",
        "page": 1,
        "fetched": 1,
        "businessInfo": "推广位",
        "feeds": [{"id": 7, "content": "正文", "template": "card", "username": "u"}],
    }

    out = apply_tool_whitelist("home", data)

    assert out["tab"] == "hot" and out["page"] == 1 and out["fetched"] == 1
    assert "businessInfo" not in out
    assert set(out["feeds"][0]) == {"id", "content", "username"}


def test_home_plain_list_still_supported() -> None:
    out = apply_tool_whitelist("home", [{"id": 1, "content": "x", "template": "card"}])
    assert out == [{"id": 1, "content": "x"}]


def test_topic_error_fields_pass_through() -> None:
    data = {
        "topic_error": "验证码",
        "feeds": [{"id": 2, "content": "y", "template": "card"}],
    }

    out = apply_tool_whitelist("topic", data)

    assert out["topic_error"] == "验证码"
    assert out["feeds"] == [{"id": 2, "content": "y"}]


# ---------------------------------------------------------------------------
# feed_detail：顶层用动态白名单，replies 树递归用评论白名单
# ---------------------------------------------------------------------------


def test_feed_detail_prunes_top_level_fields() -> None:
    """顶层只保留动态白名单字段，businessInfo / 模板类字段全部丢弃。"""
    data = {
        "id": 100,
        "title": "标题",
        "content": "正文",
        "username": "作者",
        "like_num": 12,
        "share_url": "https://www.coolapk.com/feed/100",
        "businessInfo": "推广位",
        "template": "card",
        "is_top": 1,
    }

    out = apply_tool_whitelist("feed_detail", data)

    assert out["id"] == 100
    assert out["content"] == "正文"
    assert out["like_num"] == 12
    assert out["share_url"] == "https://www.coolapk.com/feed/100"
    assert "businessInfo" not in out
    assert "template" not in out
    assert "is_top" not in out
    assert set(out) == {"id", "title", "content", "username", "like_num", "share_url"}


def test_feed_detail_prunes_replies_recursively() -> None:
    """楼中楼按评论白名单逐层裁剪，白名单外的键在每一层都消失。"""
    data = {
        "id": 1,
        "content": "主帖",
        "replies": [
            {
                "id": 11,
                "content": "一楼",
                "username": "甲",
                "junk": "广告",
                "sub_replies": [
                    {"id": 111, "content": "楼中楼", "username": "乙", "junk": "广告"},
                ],
            }
        ],
    }

    out = apply_tool_whitelist("feed_detail", data)
    reply = out["replies"][0]

    assert reply == {
        "id": 11,
        "content": "一楼",
        "username": "甲",
        "sub_replies": [{"id": 111, "content": "楼中楼", "username": "乙"}],
    }
    assert reply["sub_replies"][0] == {"id": 111, "content": "楼中楼", "username": "乙"}
    assert "junk" not in reply
    assert "junk" not in reply["sub_replies"][0]


def test_feed_detail_drops_replies_empty_after_pruning() -> None:
    """裁剪后变成空 dict 的占位/广告条目要丢弃，不能输出 {} 噪声。"""
    data = {
        "id": 1,
        "content": "主帖",
        "replies": [
            {"id": 11, "content": "正常评论"},
            {"junk": "纯广告位"},
            {"other": 1, "another": 2},
        ],
    }

    out = apply_tool_whitelist("feed_detail", data)

    assert out["replies"] == [{"id": 11, "content": "正常评论"}]
    assert all(reply for reply in out["replies"])


# ---------------------------------------------------------------------------
# search：按条目内容分发到 feed / user / app 白名单
# ---------------------------------------------------------------------------


def test_search_dispatches_feed_user_and_app_whitelists() -> None:
    """同一批结果里混着动态、用户、应用时，各自套用自己的白名单。"""
    results = [
        {"id": 1, "content": "动态正文", "like_num": 3, "junk": "x"},
        {"uid": 7, "username": "某用户", "level": 6, "junk": "x"},
        {"id": 2, "package_name": "com.example.app", "version": "1.0", "junk": "x"},
    ]

    feed, user, app = apply_tool_whitelist("search", results)

    assert feed == {"id": 1, "content": "动态正文", "like_num": 3}
    assert user == {"uid": 7, "username": "某用户", "level": 6}
    assert app == {"id": 2, "package_name": "com.example.app", "version": "1.0"}


def test_search_keeps_flag_fields() -> None:
    """suspected_ad / comment_match 这类标记字段不属于任何白名单，但必须保留。"""
    results = [
        {
            "id": 1,
            "content": "正文",
            "like_num": 0,
            "suspected_ad": True,
            "comment_match": ["关键词"],
            "junk": "x",
        }
    ]

    (out,) = apply_tool_whitelist("search", results)

    assert out["suspected_ad"] is True
    assert out["comment_match"] == ["关键词"]
    assert "junk" not in out


# ---------------------------------------------------------------------------
# get_url：保留原始 camelCase 结构，只递归剔除敏感字段
# ---------------------------------------------------------------------------


def test_get_url_strips_sensitive_keys_recursively() -> None:
    """token/session/cookie 的整键与变体键（accessToken/sessionId）都要剔除，
    普通业务字段与嵌套结构保持原样。"""
    payload = {
        "data": {
            "title": "帖子标题",
            "token": "tk",
            "accessToken": "at",
            "sessionId": "sid",
            "Set-Cookie": "SESSID=x",
            "password": "pwd",
            "nested": [{"keep": "ok", "refresh_token": "rt", "list": [1, 2]}],
        },
        "status": 0,
    }

    out = apply_tool_whitelist("get_url", payload)

    assert out["data"]["title"] == "帖子标题"
    assert out["data"]["nested"] == [{"keep": "ok", "list": [1, 2]}]
    assert out["status"] == 0
    dropped = {"token", "accessToken", "sessionId", "Set-Cookie", "password"}
    assert dropped.isdisjoint(out["data"])
    assert "refresh_token" not in out["data"]["nested"][0]


# ---------------------------------------------------------------------------
# author_digest：作者画像套 author 白名单，posts 套动态白名单
# ---------------------------------------------------------------------------


def test_author_digest_prunes_nested_author_and_posts() -> None:
    data = {
        "uid": 9,
        "username": "大佬",
        "keyword": "root",
        "pages": 3,
        "fetched": 60,
        "matched": 8,
        "junk": "x",
        "author": {
            "uid": 9,
            "username": "大佬",
            "author_tier": "top",
            "author_tier_label": "领域顶流",
            "is_expert": True,
            "fans_per_feed": 790,
            "expertise": ["解锁/Root"],
            "junk": "x",
        },
        "posts": [
            {"id": 1, "content": "高赞帖", "post_score": 305, "junk": "x"},
            {"junk": "空占位"},
        ],
    }

    out = apply_tool_whitelist("author_digest", data)

    assert out["author"] == {
        "uid": 9,
        "username": "大佬",
        "author_tier": "top",
        "author_tier_label": "领域顶流",
        "is_expert": True,
        "fans_per_feed": 790,
        "expertise": ["解锁/Root"],
    }
    assert out["posts"] == [{"id": 1, "content": "高赞帖", "post_score": 305}]
    assert out["keyword"] == "root"
    assert "junk" not in out


# ---------------------------------------------------------------------------
# 兜底：未知工具名、None、非 dict 输入
# ---------------------------------------------------------------------------


def test_unknown_tool_and_none_pass_through() -> None:
    """未知工具名原样返回同一对象（避免误伤新增输出），None 直接透传。"""
    payload = {"id": 1, "whatever": "保留", "token": "也不裁剪"}

    assert apply_tool_whitelist("brand_new_tool", payload) is payload
    assert apply_tool_whitelist("feed_detail", None) is None
    assert apply_tool_whitelist("get_url", None) is None

    # feed_detail 传入非 dict（例如直接给了 list）时不做结构假设
    raw = [{"id": 1, "junk": "x"}]
    assert apply_tool_whitelist("feed_detail", raw) is raw


def test_board_wrapper_keeps_meta_and_prunes_feeds() -> None:
    """board 包装结构：元信息保留，动态逐条裁剪"""
    payload = {
        "board": "digital",
        "page": 2,
        "fetched": 1,
        "feeds": [{"id": 1, "title": "t", "content": "c", "template": "card"}],
    }

    out = apply_tool_whitelist("board", payload)

    assert (out["board"], out["page"], out["fetched"]) == ("digital", 2, 1)
    assert out["feeds"][0]["id"] == 1 and "template" not in out["feeds"][0]


def test_secondhand_plain_list_supported() -> None:
    out = apply_tool_whitelist("secondhand", [{"id": 3, "content": "出", "junk": 1}])

    assert out == [{k: v for k, v in _out_first()}]


def _out_first():
    return {"id": 3, "content": "出"}.items()


def test_app_detail_prunes_alias_keys() -> None:
    """app_detail 白名单用 model_dump 的 snake_case 键名"""
    payload = {
        "id": 9,
        "title": "酷安",
        "package_name": "com.coolapk.market",
        "follow_num": 1,
        "junk": "x",
    }

    out = apply_tool_whitelist("app_detail", payload)

    assert out == {
        "id": 9,
        "title": "酷安",
        "package_name": "com.coolapk.market",
        "follow_num": 1,
    }


def test_notification_wrapper_and_plain_list() -> None:
    item = {"id": 1, "message": "hi", "dateline": "2023-11-15T06:13+08:00", "junk": "x"}

    wrapped = apply_tool_whitelist(
        "notification", {"ntype": "message", "page": 1, "fetched": 1, "items": [item], "counts": {"m": 2}}
    )
    assert wrapped["ntype"] == "message" and wrapped["counts"] == {"m": 2}
    assert wrapped["items"] == [{"id": 1, "message": "hi", "dateline": "2023-11-15T06:13+08:00"}]

    plain = apply_tool_whitelist("notification", [item])
    assert plain == wrapped["items"]
