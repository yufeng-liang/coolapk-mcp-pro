"""终审补测：两份审查报告（final-behavior / final-tests）点名「零覆盖 / 弱断言」的路径。

这些点此前用突变测试证明「改错了也不会红」：端点 URL、页参数归一、卡片幽灵条目、
关键词在未截断文本上匹配、搜索分页口径、多图部分失败与去重、linkparse 数字兜底边界。
断言都对着行为写，不重复既有用例。
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from click.testing import CliRunner

from coolapk_mcp import cli as cli_mod
from coolapk_mcp import client as client_mod
from coolapk_mcp import models as models_mod
from coolapk_mcp import server as server
from coolapk_mcp import throttle
from coolapk_mcp.client import CoolapkClient, CoolapkError
from coolapk_mcp.config import AppConfig
from coolapk_mcp.linkparse import parse_feed_id

_PAGE = CoolapkClient.PAGE_SIZE


def _feed_raw(index: int, message: str = "正文内容") -> dict:
    return {
        "entityType": "feed",
        "id": index,
        # uid 唯一：搜索结果按「同作者+同正文开头」去重，共用 uid 与正文会被判成重复
        "uid": 10000 + index,
        "message": message,
        "username": "作者",
        "dateline": 1700000000,
        "likenum": 1,
        "replynum": 1,
        "forwardnum": 0,
    }


def _feeds(count: int, message: str = "正文内容") -> list[dict]:
    return [_feed_raw(index + 1, message) for index in range(count)]


def _patch_client(monkeypatch: pytest.MonkeyPatch, fake: object) -> None:
    async def _fake_get_client():
        return fake

    monkeypatch.setattr(server, "_get_client", _fake_get_client)


# ---------- ① 客户端端点 URL：recommend / follow 不能混用 ----------


def _make_client(handler) -> CoolapkClient:
    """已登录 + 零限速 + MockTransport 的客户端（与 test_client.py 同范式）。"""
    config = AppConfig()
    config.uid = "10086"
    config.token = "test-token"
    config.device_code = "test-device-code"
    client = CoolapkClient(config=config)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client._limiter = throttle.RateLimiter(min_interval=0.0)
    return client


def test_recommend_and_follow_hit_different_endpoints() -> None:
    """端点口径必须钉死：recommend 走 /v6/main/indexV8，follow 走 dataList+FOLLOW。

    此前 recommend 实际请求的是 V9_HOME_TAB_FOLLOW（关注流），关注少的人拿到空列表；
    把所有 fake client 都换成「只记方法名」的桩时，改回旧端点测试依然全绿，所以这里
    直接断言真实 URL。
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"data": []})

    client = _make_client(handler)
    try:
        asyncio.run(client.get_main_feeds(2))
        asyncio.run(client.get_home_feeds(2))
    finally:
        asyncio.run(client.close())

    assert seen[0] == "https://api.coolapk.com/v6/main/indexV8?page=2"
    assert seen[1] == (
        "https://api.coolapk.com/v6/page/dataList"
        "?url=%2Fpage%3Furl%3DV9_HOME_TAB_FOLLOW&page=2"
    )


# ---------- ② 页参数归一（page=0 / 负数 / 字符串） ----------


class _RecordingClient:
    """记录 page 的假 client：home / board / secondhand / notify 共用。"""

    def __init__(self, count: int = 1) -> None:
        self.count = count
        self.pages: list[int] = []

    async def get_main_feeds(self, page: int = 1):
        self.pages.append(page)
        return _feeds(self.count)

    async def get_board_feeds(self, board_id: str, page: int = 1):
        self.pages.append(page)
        return _feeds(self.count)

    async def get_secondhand_feeds(self, brand, page: int = 1):
        self.pages.append(page)
        return _feeds(self.count)

    async def get_notifications(self, ntype: str, page: int = 1):
        self.pages.append(page)
        return [
            {"id": index, "message": "回复", "dateline": 1700000000}
            for index in range(self.count)
        ]


class _SearchFake:
    def __init__(self, items: list[dict]) -> None:
        self.items = items
        self.pages: list[int] = []

    async def search(self, keyword, search_type="feed", page=1, sort="hot"):
        self.pages.append(page)
        return self.items


@pytest.mark.parametrize("value,expected", [(0, 1), (-3, 1), ("2", 2), (None, 1)])
@pytest.mark.parametrize("tool", ["home", "board", "secondhand", "notify", "search"])
def test_page_argument_is_normalized(monkeypatch, tool, value, expected) -> None:
    """5 处 max(1, int(page or 1)) 都要有行为锁定：0/负数回 1，字符串能转成 int。"""
    if tool == "search":
        fake: object = _SearchFake(_feeds(1))
        _patch_client(monkeypatch, fake)
        payload = json.loads(asyncio.run(server.coolapk_search("正文", page=value)))
    else:
        fake = _RecordingClient(count=1)
        _patch_client(monkeypatch, fake)
        calls = {
            "home": lambda: server.coolapk_home("recommend", value),
            "board": lambda: server.coolapk_board("digital", value),
            "secondhand": lambda: server.coolapk_secondhand(1005, value),
            "notify": lambda: server.coolapk_notify("message", value),
        }
        payload = json.loads(asyncio.run(calls[tool]()))

    assert fake.pages == [expected]
    assert payload["page"] == expected


# ---------- ③ 列表条目：卡片幽灵条目与页填充 ----------


class _FixedClient:
    def __init__(self, raw: list[dict]) -> None:
        self.raw = raw

    async def get_main_feeds(self, page: int = 1):
        return self.raw


def test_feed_list_drops_card_ghosts_but_keeps_nested_feeds(monkeypatch) -> None:
    """运营卡片不能变成 id=null 的幽灵条目，卡片里嵌的动态也不能丢。"""
    _patch_client(
        monkeypatch,
        _FixedClient(
            [
                _feed_raw(1),
                {"entityType": "card", "title": "优质图文", "entities": [_feed_raw(2)]},
                {"entityType": "card", "id": 99, "title": "广告位"},
            ]
        ),
    )

    payload = json.loads(asyncio.run(server.coolapk_home("recommend")))

    assert [feed["id"] for feed in payload["feeds"]] == [1, 2]
    assert payload["fetched"] == 2
    assert payload["has_more"] is False, "3 条原始条目没填满一页，不该说还有下一页"


def test_page_fill_counts_cards_as_placeholders(monkeypatch) -> None:
    """整页卡片（解析后 0 条动态）仍算填满：has_more 按接口这一页判定，不看解析结果。"""
    _patch_client(
        monkeypatch,
        _FixedClient([{"entityType": "card", "id": index} for index in range(_PAGE)]),
    )

    payload = json.loads(asyncio.run(server.coolapk_home("recommend")))

    assert payload["fetched"] == 0
    assert payload["has_more"] is True
    assert payload["next_page"] == 2


def test_home_marks_unknown_tab_fallback(monkeypatch) -> None:
    """未知 tab 静默回落会让调用方以为参数生效了：必须回显 tab_fallback。"""
    _patch_client(monkeypatch, _RecordingClient(count=1))

    unknown = json.loads(asyncio.run(server.coolapk_home("不存在的分栏")))
    known = json.loads(asyncio.run(server.coolapk_home("recommend")))

    assert unknown["tab"] == "recommend"
    assert unknown["tab_fallback"] is True
    assert "tab_fallback" not in known


# ---------- ④ 搜索：分页口径与空结果解释 ----------


def test_search_paging_uses_raw_count(monkeypatch) -> None:
    """分页按原始条数判：搜索每页约 20 条，按结果数或 PAGE_SIZE 判都会错。"""
    items = _feeds(8, "解锁工具") + [_feed_raw(100 + i, "无关内容") for i in range(12)]
    fake = _SearchFake(items)
    _patch_client(monkeypatch, fake)

    payload = json.loads(asyncio.run(server.coolapk_search("解锁")))

    assert payload["counts"]["raw_count"] == 20
    assert payload["counts"]["keyword_filtered"] == 12
    assert len(payload["results"]) == 8
    assert payload["has_more"] is True
    assert payload["next_page"] == 2


def test_search_mid_page_is_last_page(monkeypatch) -> None:
    """18 条全部命中但没填满搜索页（20）→ 末页，不该再给 next_page 让用户白翻。"""
    _patch_client(monkeypatch, _SearchFake(_feeds(18, "解锁工具")))

    payload = json.loads(asyncio.run(server.coolapk_search("解锁")))

    assert payload["counts"]["raw_count"] == 18
    assert len(payload["results"]) == 18
    assert payload["has_more"] is False
    assert payload["next_page"] is None


def test_search_hint_explains_card_only_result(monkeypatch) -> None:
    """只剩卡片/广告位时不能建议 strict=false：那是重试一次、结果完全一样。"""
    _patch_client(
        monkeypatch,
        _SearchFake([{"entityType": "card", "id": index, "title": "广告位"} for index in range(5)]),
    )

    payload = json.loads(asyncio.run(server.coolapk_search("解锁")))

    assert payload["results"] == []
    assert payload["counts"]["cards_removed"] == 5
    assert "这与 strict 无关" in payload["hint"]
    assert "strict=false 查看原始结果" not in payload["hint"]


# ---------- ⑤ 多图读取：部分失败与去重 ----------


class _FlakyImageClient:
    def __init__(self, fail_marker: str = "") -> None:
        self.fail_marker = fail_marker
        self.seen: list[str] = []

    async def download_image(self, url: str):
        self.seen.append(url)
        if self.fail_marker and self.fail_marker in url:
            raise CoolapkError("上游不可用", error_code=502, kind="network")
        return b"\x89PNG-fake", "png"


def test_read_image_reports_which_image_failed(monkeypatch) -> None:
    """部分失败必须指明第几张，且不再继续下载后面的：否则用户以为这帖只有那么多图。"""
    fake = _FlakyImageClient(fail_marker="b.png")
    _patch_client(monkeypatch, fake)
    urls = " ".join(f"https://image.coolapk.com/{name}" for name in ("a.png", "b.png", "c.png"))

    payload = json.loads(asyncio.run(server.coolapk_read_image(urls)))

    assert payload["code"] == 502
    assert "第 2/3 张" in payload["error"]
    assert fake.seen == ["https://image.coolapk.com/a.png", "https://image.coolapk.com/b.png"]


def test_read_image_deduplicates_repeated_urls(monkeypatch) -> None:
    """同一张图粘两遍不该下载两次（限速窗口很贵）。"""
    fake = _FlakyImageClient()
    _patch_client(monkeypatch, fake)

    images = asyncio.run(
        server.coolapk_read_image(
            "https://image.coolapk.com/a.png https://image.coolapk.com/a.png "
            "https://image.coolapk.com/b.png"
        )
    )

    assert fake.seen == ["https://image.coolapk.com/a.png", "https://image.coolapk.com/b.png"]
    assert len(list(images)) == 2


# ---------- ⑥ 关键词匹配与上限：未截断文本、分页阈值 ----------


def test_keyword_matches_before_content_truncation() -> None:
    """关键词在超长正文末尾也要命中：按截断后的 content 匹配会漏（实测 2100 字）。"""
    long_message = "开头内容" + "填充" * (models_mod.FEED_CONTENT_MAX_CHARS // 2) + "尾部关键词"
    assert len(long_message) > models_mod.FEED_CONTENT_MAX_CHARS
    raw = [_feed_raw(1, long_message), _feed_raw(2, "无关内容")]

    items, total = server._prepare_feeds(raw, "尾部关键词", "hot")

    assert total == 2
    assert [item["id"] for item in items] == [1]


def test_paging_meta_respects_explicit_size() -> None:
    """size<=0 关闭分页提示；显式 size 按它判（搜索页 20 与列表页 10 不同）。"""
    assert server._paging_meta(3, _PAGE, size=0) == {"has_more": False, "next_page": None}
    assert server._paging_meta(1, 19, size=20) == {"has_more": False, "next_page": None}
    assert server._paging_meta(1, 20, size=20) == {"has_more": True, "next_page": 2}
    assert server._paging_meta(2, _PAGE) == {"has_more": True, "next_page": 3}


def test_env_int_survives_overflow(monkeypatch) -> None:
    """COOLAPK_PAGE_SIZE=inf 之类不能把 import 期搞崩：非法值一律回落默认。"""
    monkeypatch.setenv("COOLAPK_PAGE_SIZE", "inf")
    assert client_mod._env_int("COOLAPK_PAGE_SIZE", 10) == 10
    monkeypatch.setenv("COOLAPK_PAGE_SIZE", "1e400")
    assert client_mod._env_int("COOLAPK_PAGE_SIZE", 10) == 10
    monkeypatch.setenv("COOLAPK_REPLY_MAX_ITEMS", "inf")
    assert models_mod._env_int("COOLAPK_REPLY_MAX_ITEMS", 60) == 60


# ---------- ⑦ CLI 首页分页元数据 ----------


def test_cli_home_reports_paging_meta(monkeypatch) -> None:
    """CLI home 与 server 同口径：包装里必须有 has_more/next_page。"""

    class _CliHome:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get_main_feeds(self, page: int = 1):
            return _feeds(_PAGE)

    monkeypatch.setattr(cli_mod, "CoolapkClient", lambda *a, **k: _CliHome())

    result = CliRunner().invoke(cli_mod.cli, ["home", "--tab", "recommend"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["tab"] == "recommend"
    assert payload["fetched"] == _PAGE
    assert payload["has_more"] is True
    assert payload["next_page"] == 2


# ---------- ⑧ linkparse 数字兜底边界 ----------


@pytest.mark.parametrize(
    "value,expected",
    [
        # 外部站点链接不做数字兜底：GitHub issue 里的 123456 曾被当成帖子上报
        ("https://github.com/foo/bar/issues/123456", None),
        ("https://example.com/a/123456/whatever", None),
        # 有酷安链接就只信链接，外部链接不参与判定
        ("https://www.coolapk.com/feed/1111111 https://github.com/a/b/issues/999", 1111111),
        # 缺域名的路径不算链接，4 位数字也够不上 feed id
        ("/m/feed/detail?id=8899", None),
        # 正文里的手机号（11 位）不是 feed id
        ("手机号 13800000000 打给我", None),
        ("看这两条 1234567 和 7654321", None),
        ("123456", 123456),
        ("123456789", 123456789),
        # 纯数字是显式输入，按原样采纳：6-9 位规则只作用于整段文案里的数字段，
        # 用户明确点名的一串数字不该被二次评判（手机号只在文案出现时才会被拦）
        ("12345", 12345),
        ("1234567890", 1234567890),
    ],
)
def test_parse_feed_id_boundaries(value, expected) -> None:
    assert parse_feed_id(value) == expected
