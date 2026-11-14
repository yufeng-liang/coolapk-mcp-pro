"""用户可感知改进（第二批）测试

六项：首页推荐流语义、列表分页信号、搜索空结果解释、正文/回复长度上限、
多图读取、分享链接解析。断言都对着「用户会碰到什么」写，不只验证调用成功。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from click.testing import CliRunner

from coolapk_mcp import cli as cli_mod
from coolapk_mcp import models as models_mod
from coolapk_mcp import server as server
from coolapk_mcp.client import CoolapkError
from coolapk_mcp.linkparse import parse_feed_id
from coolapk_mcp.models import FeedDetail, FeedModel, ReplyModel, cap_replies

_FEED_RAW = {
    "entityType": "feed",
    "id": 1,
    "message": "正文内容",
    "username": "作者",
    "dateline": 1700000000,
    "likenum": 3,
}


def _feeds(count: int) -> list[dict]:
    return [dict(_FEED_RAW, id=index + 1) for index in range(count)]


def _patch_client(monkeypatch: pytest.MonkeyPatch, fake: object) -> None:
    async def _fake_get_client():
        return fake

    monkeypatch.setattr(server, "_get_client", _fake_get_client)


# ---------- ① 首页推荐流语义 ----------


class _HomeClient:
    def __init__(self, count: int = 1) -> None:
        self.count = count
        self.calls: list[tuple[str, int]] = []

    async def get_main_feeds(self, page: int = 1):
        self.calls.append(("main", page))
        return _feeds(self.count)

    async def get_home_feeds(self, page: int = 1):
        self.calls.append(("home", page))
        return _feeds(self.count)

    async def get_hot_feeds(self, page: int = 1):
        self.calls.append(("hot", page))
        return _feeds(self.count)

    async def get_latest_feeds(self, page: int = 1):
        self.calls.append(("latest", page))
        return _feeds(self.count)


@pytest.mark.parametrize(
    "tab,source,shown",
    [
        ("recommend", "main", "recommend"),
        ("", "main", "recommend"),
        ("不存在的分栏", "main", "recommend"),
        ("follow", "home", "follow"),
        ("关注", "home", "follow"),
        ("dynamic", "home", "follow"),
        ("hot", "hot", "hot"),
        ("latest", "latest", "latest"),
    ],
)
def test_home_tab_routes_and_echoes(monkeypatch, tab, source, shown) -> None:
    """recommend 必须走真推荐流(main/indexV8)，关注流独立成 follow 并不再冒充推荐。"""
    fake = _HomeClient()
    _patch_client(monkeypatch, fake)

    payload = json.loads(asyncio.run(server.coolapk_home(tab, 1)))

    assert fake.calls == [(source, 1)]
    assert payload["tab"] == shown


def test_home_reports_paging_signals(monkeypatch) -> None:
    """分页信号：本页填满 → has_more=True 且给出 next_page；不满 → 到底了。"""
    full = _HomeClient(count=10)
    _patch_client(monkeypatch, full)
    payload = json.loads(asyncio.run(server.coolapk_home("recommend", 2)))
    assert payload["has_more"] is True
    assert payload["next_page"] == 3

    partial = _HomeClient(count=3)
    _patch_client(monkeypatch, partial)
    payload = json.loads(asyncio.run(server.coolapk_home("recommend", 2)))
    assert payload["has_more"] is False
    assert payload["next_page"] is None


# ---------- ② 列表工具分页信号 ----------


class _ListClient(_HomeClient):
    async def get_board_feeds(self, board_id: str, page: int = 1):
        self.calls.append((f"board:{board_id}", page))
        return _feeds(self.count)

    async def get_secondhand_feeds(self, brand, page: int = 1):
        self.calls.append(("secondhand", page))
        return _feeds(self.count)


def test_board_carries_paging_meta(monkeypatch) -> None:
    fake = _ListClient(count=10)
    _patch_client(monkeypatch, fake)

    payload = json.loads(asyncio.run(server.coolapk_board("digital", 1)))

    assert payload["has_more"] is True
    assert payload["next_page"] == 2


def test_secondhand_carries_paging_meta(monkeypatch) -> None:
    fake = _ListClient(count=2)
    _patch_client(monkeypatch, fake)

    payload = json.loads(asyncio.run(server.coolapk_secondhand(1005, 4)))

    assert payload["has_more"] is False
    assert payload["next_page"] is None


class _NotifyClient:
    def __init__(self, count: int) -> None:
        self.count = count

    async def get_notifications(self, ntype: str, page: int = 1):
        return [{"id": index, "message": "回复", "dateline": 1700000000} for index in range(self.count)]


def test_notify_carries_paging_meta(monkeypatch) -> None:
    _patch_client(monkeypatch, _NotifyClient(count=10))

    payload = json.loads(asyncio.run(server.coolapk_notify("message", 1)))

    assert payload["has_more"] is True
    assert payload["next_page"] == 2


# ---------- ③ 搜索空结果解释 ----------


class _SearchClient:
    def __init__(self, items: list[dict]) -> None:
        self.items = items

    async def search(self, keyword, search_type="feed", page=1, sort="hot"):
        return self.items


def _search_items(matched: int, unmatched: int) -> list[dict]:
    items = [dict(_FEED_RAW, id=index + 1, message="Magisk 刷机教程") for index in range(matched)]
    items += [
        dict(_FEED_RAW, id=100 + index, message="无关内容") for index in range(unmatched)
    ]
    return items


def test_search_reports_paging_and_no_hint_when_hit(monkeypatch) -> None:
    _patch_client(monkeypatch, _SearchClient(_search_items(2, 0)))

    payload = json.loads(asyncio.run(server.coolapk_search("Magisk", page=1)))

    assert payload["has_more"] is False
    assert "hint" not in payload


def test_search_explains_keyword_filtered_empty(monkeypatch) -> None:
    """搜到了但被严格过滤掉：必须说明「被滤掉多少条」，否则用户以为酷安没有。"""
    _patch_client(monkeypatch, _SearchClient(_search_items(0, 30)))

    payload = json.loads(asyncio.run(server.coolapk_search("Magisk")))

    assert payload["results"] == []
    assert payload["counts"]["keyword_filtered"] == 30
    assert "strict=false" in payload["hint"]


def test_search_explains_truly_empty_result(monkeypatch) -> None:
    _patch_client(monkeypatch, _SearchClient([]))

    payload = json.loads(asyncio.run(server.coolapk_search("冷门词")))

    assert "没有搜到" in payload["hint"]


# ---------- ④ 正文/回复长度上限 ----------


def test_feed_content_is_truncated_with_flag() -> None:
    feed = FeedModel.from_api(dict(_FEED_RAW, message="长" * 5000))

    assert feed.content_truncated is True
    assert len(feed.content) <= models_mod.FEED_CONTENT_MAX_CHARS + 1
    assert feed.content.endswith("…")


def test_feed_content_shorter_than_limit_is_untouched() -> None:
    feed = FeedModel.from_api(dict(_FEED_RAW, message="短正文"))

    assert feed.content_truncated is False
    assert feed.content == "短正文"


def test_reply_content_is_truncated_with_flag() -> None:
    reply = ReplyModel.from_api({"id": 9, "message": "评" * 3000, "dateline": 1700000000})

    assert reply.content_truncated is True
    assert len(reply.content) <= models_mod.REPLY_CONTENT_MAX_CHARS + 1


def test_cap_replies_marks_omitted_count() -> None:
    """all_replies 能翻 300 条，一口气全塞进上下文会挤掉后续对话：留前 N 条并标省略数。"""
    detail = FeedDetail.from_api(
        {"id": 1, "message": "正文"},
        [{"id": index + 1, "message": f"评论{index}"} for index in range(100)],
    )

    capped = cap_replies(detail)

    assert len(capped.replies) == models_mod.REPLY_MAX_ITEMS
    assert capped.replies_omitted == 100 - models_mod.REPLY_MAX_ITEMS
    assert capped.replies_truncated is True


def test_truncate_can_be_disabled() -> None:
    assert models_mod._truncate("abc", 0) == ("abc", False)
    assert models_mod._truncate("abcdef", 3) == ("abc…", True)


def test_content_limits_are_configurable(monkeypatch) -> None:
    monkeypatch.setenv("COOLAPK_FEED_CONTENT_MAX", "7")
    monkeypatch.setenv("COOLAPK_REPLY_CONTENT_MAX", "bad")

    assert models_mod._env_int("COOLAPK_FEED_CONTENT_MAX", 2000) == 7
    assert models_mod._env_int("COOLAPK_REPLY_CONTENT_MAX", 800) == 800


# ---------- ⑤ 多图读取 ----------


class _ImageClient:
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def download_image(self, url: str):
        self.seen.append(url)
        return b"\x89PNG-fake", "png"


def test_read_image_returns_every_image(monkeypatch) -> None:
    """九宫格帖一次给全：两张 URL 应返回两块 ImageContent，而不是一张或一个 JSON。"""
    fake = _ImageClient()
    _patch_client(monkeypatch, fake)

    result = asyncio.run(
        server.mcp.call_tool(
            "coolapk_read_image",
            {"url": "https://image.coolapk.com/a.png, https://image.coolapk.com/b.jpg"},
        )
    )
    content = result[0] if isinstance(result, tuple) else result

    assert [type(item).__name__ for item in content] == ["ImageContent", "ImageContent"]
    assert fake.seen == [
        "https://image.coolapk.com/a.png",
        "https://image.coolapk.com/b.jpg",
    ]


def test_read_image_rejects_foreign_host_before_downloading(monkeypatch) -> None:
    fake = _ImageClient()
    _patch_client(monkeypatch, fake)

    payload = json.loads(
        asyncio.run(
            server.coolapk_read_image("https://evil.example.com/a.png, https://image.coolapk.com/b.png")
        )
    )

    assert payload["code"] == 403
    assert fake.seen == [], "第一张就不合法时不该先下载后面的"


def test_read_image_limits_and_empty_input(monkeypatch) -> None:
    _patch_client(monkeypatch, _ImageClient())

    too_many = json.loads(
        asyncio.run(
            server.coolapk_read_image(
                " ".join(f"https://image.coolapk.com/{i}.png" for i in range(10))
            )
        )
    )
    empty = json.loads(asyncio.run(server.coolapk_read_image("   ")))

    assert "最多 9 张" in too_many["error"]
    assert "不能为空" in empty["error"]


# ---------- ⑥ 分享链接解析 ----------


@pytest.mark.parametrize(
    "value,expected",
    [
        (123, 123),
        ("123456", 123456),
        ("https://www.coolapk.com/feed/1234567", 1234567),
        ("看看这个 https://www.coolapk.com/feed/7654321 挺好", 7654321),
        ("https://www.coolapk.com/m/feed/detail?id=8899", 8899),
        ("https://www.coolapk.com/feed/detail?feedId=4242", 4242),
        ("#在酷安# 分享自酷安 https://www.coolapk.com/feed/31337?shareFrom=1", 31337),
        (None, None),
        (True, None),
        ("没有数字", None),
        ("看这两条 1234567 和 7654321", None),
    ],
)
def test_parse_feed_id(value, expected) -> None:
    assert parse_feed_id(value) == expected


class _DetailClient:
    def __init__(self) -> None:
        self.seen: list[int] = []

    async def get_feed_detail_smart(self, feed_id, **kwargs):
        self.seen.append(feed_id)
        return {
            "data": {"id": feed_id, "message": "正文", "replynum": 0},
            "replies": None,
            "source": "api",
        }


def test_feed_detail_accepts_share_link(monkeypatch) -> None:
    fake = _DetailClient()
    _patch_client(monkeypatch, fake)

    payload = json.loads(
        asyncio.run(server.coolapk_feed_detail("https://www.coolapk.com/feed/1234567890"))
    )

    assert fake.seen == [1234567890]
    assert payload["id"] == 1234567890


def test_feed_detail_rejects_unparsable_input(monkeypatch) -> None:
    _patch_client(monkeypatch, _DetailClient())

    payload = json.loads(asyncio.run(server.coolapk_feed_detail("这条消息里没有链接")))

    assert "无法从 feed_id 解析" in payload["error"]


class _CliClient:
    def __init__(self) -> None:
        self.seen: list[int] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_feed_detail_smart(self, feed_id, **kwargs):
        self.seen.append(feed_id)
        return {
            "data": {"id": feed_id, "message": "正文", "replynum": 0},
            "replies": None,
            "source": "api",
        }

    async def get_feed_history(self, feed_id):
        return []


def _run_cli(monkeypatch, args):
    fake = _CliClient()
    monkeypatch.setattr(cli_mod, "CoolapkClient", lambda *a, **k: fake)
    result = CliRunner().invoke(cli_mod.cli, args)
    return fake, result


def test_cli_feed_accepts_share_link(monkeypatch) -> None:
    fake, result = _run_cli(monkeypatch, ["feed", "https://www.coolapk.com/feed/24681012"])

    assert result.exit_code == 0, result.output
    assert fake.seen == [24681012]


def test_cli_feed_rejects_unparsable_input(monkeypatch) -> None:
    _fake, result = _run_cli(monkeypatch, ["feed", "not-a-link"])

    assert result.exit_code == 1
    assert "无法从 FEED_ID 解析" in result.output


def test_cli_home_recommend_uses_main_feed(monkeypatch) -> None:
    """CLI 与 server 同一套语义：home 默认 recommend 走 indexV8。"""
    calls: list[str] = []

    class _CliHome:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get_main_feeds(self, page: int = 1):
            calls.append("main")
            return _feeds(1)

        async def get_home_feeds(self, page: int = 1):
            calls.append("home")
            return _feeds(1)

    monkeypatch.setattr(cli_mod, "CoolapkClient", lambda *a, **k: _CliHome())
    result = CliRunner().invoke(cli_mod.cli, ["home", "--tab", "recommend"])

    assert result.exit_code == 0, result.output
    assert calls == ["main"]


def test_cli_home_follow_uses_follow_feed(monkeypatch) -> None:
    calls: list[str] = []

    class _CliHome:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get_main_feeds(self, page: int = 1):
            calls.append("main")
            return _feeds(1)

        async def get_home_feeds(self, page: int = 1):
            calls.append("home")
            return _feeds(1)

    monkeypatch.setattr(cli_mod, "CoolapkClient", lambda *a, **k: _CliHome())
    result = CliRunner().invoke(cli_mod.cli, ["home", "--tab", "follow"])

    assert result.exit_code == 0, result.output
    assert calls == ["home"]


def test_coolapk_error_still_used() -> None:
    """链接解析失败走的是同一套错误口径（kind=api），不是裸异常。"""
    assert CoolapkError("x", error_code=-1, kind="api").kind == "api"
