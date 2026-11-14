"""评论聚焦模块（focus.collect_focus）的去重与预算行为测试。

全部走假 client，不发真实网络请求。
"""

import asyncio

from coolapk_mcp import focus as focus_mod
from coolapk_mcp.budget import BudgetExceeded, FetchResult
from coolapk_mcp.client import CoolapkError


def _reply(rid, username="某人"):
    return {"id": rid, "username": username, "message": "评论"}


class _FakeFocusClient:
    def __init__(self, author=None, hot=None, author_exc=None, hot_exc=None):
        self._author = author if author is not None else []
        self._hot = hot if hot is not None else []
        self._author_exc = author_exc
        self._hot_exc = hot_exc
        self.budgets = []
        self.start_pages: list[int] = []

    async def get_author_replies_paged(
        self, feed_id, max_pages=5, budget=None, start_page=1
    ):
        self.budgets.append(budget)
        self.start_pages.append(start_page)
        if self._author_exc is not None:
            raise self._author_exc
        return FetchResult(self._author, truncated=False, next_page=None, requests=1)

    async def get_popular_replies_paged(
        self, feed_id, max_pages=5, budget=None, start_page=1
    ):
        self.budgets.append(budget)
        self.start_pages.append(start_page)
        if self._hot_exc is not None:
            raise self._hot_exc
        return FetchResult(self._hot, truncated=False, next_page=None, requests=1)


def test_focus_mode_default_returns_empty() -> None:
    client = _FakeFocusClient()
    merged, meta = asyncio.run(focus_mod.collect_focus(client, 1, "default"))
    assert merged == [] and meta is None and client.budgets == []


def test_focus_merges_and_dedupes_by_id() -> None:
    client = _FakeFocusClient(author=[_reply(1), _reply(2)], hot=[_reply(2), _reply(3)])
    merged, meta = asyncio.run(focus_mod.collect_focus(client, 1, "smart"))

    assert [r["id"] for r in merged] == [1, 2, 3]
    assert meta["author_replies"] == 2 and meta["hot_replies"] == 2
    assert meta["total_shown"] == 3
    # smart 两路共用同一份预算
    assert client.budgets[0] is client.budgets[1]


def test_focus_keeps_replies_without_id() -> None:
    """缺 id 的条目不能共用 "None" 键被去重吃掉。"""
    client = _FakeFocusClient(
        author=[{"username": "甲", "message": "a"}, {"username": "乙", "message": "b"}],
        hot=[],
    )
    merged, meta = asyncio.run(focus_mod.collect_focus(client, 1, "author"))

    assert len(merged) == 2
    assert meta["total_shown"] == 2


def test_focus_degrades_on_coolapk_error() -> None:
    client = _FakeFocusClient(author=[_reply(1)], hot_exc=CoolapkError("挂了", kind="api"))
    merged, meta = asyncio.run(focus_mod.collect_focus(client, 1, "smart"))

    assert [r["id"] for r in merged] == [1]
    assert "失败" in meta["error"]
    # 少了一整路来源绝不能宣称已取完
    assert meta["truncated"] is True
    assert meta["missing_sources"] == ["hot"]


def test_focus_passes_start_page_to_both_sources() -> None:
    """续抓：start_page 必须透传到两个来源的 paged 方法。"""
    client = _FakeFocusClient(author=[_reply(1)], hot=[])
    asyncio.run(focus_mod.collect_focus(client, 1, "smart", start_page=3))

    assert client.start_pages == [3, 3]


def test_focus_budget_exceeded_marks_truncated() -> None:
    class _BudgetEater:
        async def get_author_replies_paged(
            self, feed_id, max_pages=5, budget=None, start_page=1
        ):
            raise BudgetExceeded("预算耗尽")

        async def get_popular_replies_paged(
            self, feed_id, max_pages=5, budget=None, start_page=1
        ):
            return FetchResult([_reply(7)], truncated=False, next_page=None, requests=1)

    merged, meta = asyncio.run(focus_mod.collect_focus(_BudgetEater(), 1, "smart"))

    assert [r["id"] for r in merged] == [7]
    assert meta["truncated"] is True
    # 两路都没有 next_page 可续抓时回到起始页，重复页由去重与整页提前停兜住
    assert meta["next_page"] == 1
