"""新增工具（board / secondhand / app_detail / notify）的返回结构与白名单测试。

全部走假 client，不发真实网络请求。
"""

import asyncio
import json

import pytest

from coolapk_mcp import server
from coolapk_mcp.client import CoolapkError

_FEED = {
    "entityType": "feed",
    "id": 1,
    "message": "正文",
    "username": "作者",
    "dateline": 1700000000,
    "likenum": 3,
    "template": "card",  # 酷安列表里的卡片标记，不应进输出
}


class _FakeToolsClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.fail_notify_count = False

    async def get_board_feeds(self, board, page=1):
        self.calls.append(("board", (board, page)))
        return [_FEED]

    async def get_secondhand_feeds(self, brand=None, page=1):
        self.calls.append(("secondhand", (brand, page)))
        return [_FEED]

    async def get_app_detail(self, package_name):
        self.calls.append(("app", (package_name,)))
        return {
            "id": 9,
            "title": "酷安",
            "pkgname": package_name,
            "description": "应用简介",
            "followNum": 10,
            "downloadCount": 20,
            "versionName": "16.2.0",
            "junk": "不该出现",
        }

    async def get_notifications(self, ntype, page=1):
        self.calls.append(("notify", (ntype, page)))
        return [
            {
                "id": 1,
                "uid": 2,
                "username": "小明",
                "message": "在吗",
                "dateline": 1700000000,
                "ukey": "u1",
                "junk": "不该出现",
            }
        ]

    async def get_notification_count(self):
        if self.fail_notify_count:
            raise CoolapkError("统计失败", kind="api")
        return {"message": 3, "comment": 1}


def _patch(monkeypatch: pytest.MonkeyPatch, fake: object) -> None:
    async def _fake_client():
        return fake

    monkeypatch.setattr(server, "_get_client", _fake_client)


def test_board_resolves_id_and_prunes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeToolsClient()
    _patch(monkeypatch, fake)

    payload = json.loads(asyncio.run(server.coolapk_board("digital", 2)))

    assert fake.calls == [("board", ("digital", 2))]
    assert payload["board"] == "digital" and payload["page"] == 2
    assert payload["fetched"] == 1
    assert set(payload["feeds"][0]) <= {
        "id", "title", "content", "tag", "feed_type", "username", "uid",
        "like_num", "reply_num", "forward_num", "read_num", "dateline",
        "device", "ip_location", "images", "post_score", "discussion_ratio",
    }
    assert "template" not in payload["feeds"][0]


def test_board_accepts_page_name(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeToolsClient()
    _patch(monkeypatch, fake)

    payload = json.loads(asyncio.run(server.coolapk_board("V10_DIGITAL_HOME")))

    assert payload["board"] == "digital"


def test_board_unknown_returns_error_with_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeToolsClient()
    _patch(monkeypatch, fake)

    payload = json.loads(asyncio.run(server.coolapk_board("not-a-board")))

    assert "未知板块" in payload["error"]
    assert "digital" in payload["error"]
    assert fake.calls == []


def test_secondhand_passes_brand(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeToolsClient()
    _patch(monkeypatch, fake)

    payload = json.loads(asyncio.run(server.coolapk_secondhand(1005, 1)))

    assert fake.calls == [("secondhand", (1005, 1))]
    assert payload["brand"] == 1005 and payload["fetched"] == 1


def test_secondhand_zero_brand_means_all(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeToolsClient()
    _patch(monkeypatch, fake)

    payload = json.loads(asyncio.run(server.coolapk_secondhand()))

    assert fake.calls == [("secondhand", (None, 1))]
    assert payload["brand"] is None


def test_app_detail_prunes_and_flattens_list(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeToolsClient()
    _patch(monkeypatch, fake)

    payload = json.loads(asyncio.run(server.coolapk_app_detail("com.coolapk.market")))

    assert payload["package_name"] == "com.coolapk.market"
    assert payload["version"] == "16.2.0"
    assert payload["follow_num"] == 10 and payload["download_num"] == 20
    assert "junk" not in payload


def test_app_detail_requires_package(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, _FakeToolsClient())

    payload = json.loads(asyncio.run(server.coolapk_app_detail("  ")))

    assert "package_name" in payload["error"]


def test_notify_default_message_and_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeToolsClient()
    _patch(monkeypatch, fake)

    payload = json.loads(asyncio.run(server.coolapk_notify("message", 1, True)))

    assert fake.calls[0] == ("notify", ("message", 1))
    assert payload["ntype"] == "message" and payload["fetched"] == 1
    assert payload["counts"] == {"message": 3, "comment": 1}
    item = payload["items"][0]
    assert item["message"] == "在吗" and "junk" not in item


def test_notify_count_failure_keeps_items(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeToolsClient()
    fake.fail_notify_count = True
    _patch(monkeypatch, fake)

    payload = json.loads(asyncio.run(server.coolapk_notify("comment", 1, True)))

    assert payload["items"] and payload["counts"]["error"]


def test_notify_unknown_type_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeToolsClient()
    _patch(monkeypatch, fake)

    payload = json.loads(asyncio.run(server.coolapk_notify("nope")))

    assert "未知通知类型" in payload["error"]
    assert "message" in payload["error"]
    assert fake.calls == []


def test_board_accepts_chinese_title(monkeypatch: pytest.MonkeyPatch) -> None:
    """README 与工具描述都写了支持中文板块名，此前传「手机」会被判未知板块。"""
    fake = _FakeToolsClient()
    _patch(monkeypatch, fake)

    payload = json.loads(asyncio.run(server.coolapk_board("手机")))

    assert payload["board"] == "phone"
    assert fake.calls == [("board", ("phone", 1))]


def test_notify_dateline_is_iso_cst(monkeypatch: pytest.MonkeyPatch) -> None:
    """通知条目 dateline 与其它工具同口径：ISO8601 + 东八区。"""
    fake = _FakeToolsClient()
    _patch(monkeypatch, fake)

    payload = json.loads(asyncio.run(server.coolapk_notify("message")))

    assert payload["items"][0]["dateline"] == "2023-11-15T06:13+08:00"
