"""budget.py 回归测试：请求/时间预算与 FetchResult 的分页元数据。

三个关注点：
1) Budget.take 的两道闸门（请求数、时间）在边界值上的行为；
2) 抛出的异常类型与消息内容（调用方靠它决定 truncated）；
3) FetchResult 作为 list 子类必须与 list 完全兼容（老调用方零改动）。
"""

from __future__ import annotations

import json
import types

import pytest

from coolapk_mcp import budget
from coolapk_mcp.budget import Budget, BudgetExceeded, FetchResult


def _fake_time(monkeypatch, value: float) -> None:
    """把 budget 模块名下的 time 换成受控替身。

    只替换 budget.time 这个模块级别的名字，不动全局 time 模块——否则会影响
    同进程里所有依赖真实时钟的代码（pytest 插件、asyncio 等）。
    """
    monkeypatch.setattr(budget, "time", types.SimpleNamespace(monotonic=lambda: value))


# ---------------------------------------------------------------------------
# Budget.take：请求数闸门
# ---------------------------------------------------------------------------


def test_take_allows_max_requests_then_raises() -> None:
    """上限 2 次：前两次放行并累加计数，第三次抛 BudgetExceeded。"""
    b = Budget(max_requests=2)
    b.take()
    b.take()
    assert b.used == 2

    with pytest.raises(BudgetExceeded) as exc:
        b.take()
    assert "请求数预算耗尽" in str(exc.value)
    assert "2" in str(exc.value)
    # 抛异常的这一跳不能污染计数，否则调用方无法区分「已用次数」
    assert b.used == 2


def test_take_zero_max_requests_raises_on_first_call() -> None:
    """max_requests=0 表示一次请求都不许发，首次 take 就要抛。"""
    b = Budget(max_requests=0)
    with pytest.raises(BudgetExceeded):
        b.take()
    assert b.used == 0


# ---------------------------------------------------------------------------
# Budget.take：时间闸门
# ---------------------------------------------------------------------------


def test_take_raises_when_deadline_exceeded(monkeypatch) -> None:
    """已耗时 10s > deadline 5s：抛时间预算异常，且不再累加 used。"""
    _fake_time(monkeypatch, 1010.0)
    b = Budget(deadline=5.0, started=1000.0)

    with pytest.raises(BudgetExceeded) as exc:
        b.take()
    assert "时间预算耗尽" in str(exc.value)
    assert b.used == 0


def test_take_deadline_boundary_is_inclusive(monkeypatch) -> None:
    """elapsed 恰好等于 deadline 即视为耗尽（代码用 >=），同时验证 4.9s 仍放行。"""
    b = Budget(deadline=5.0, started=1000.0)

    _fake_time(monkeypatch, 1004.9)
    b.take()
    assert b.used == 1

    _fake_time(monkeypatch, 1005.0)
    with pytest.raises(BudgetExceeded):
        b.take()


def test_request_budget_is_checked_before_deadline(monkeypatch) -> None:
    """两道闸门同时超限时报请求数，消息里不能是时间预算的文案。"""
    _fake_time(monkeypatch, 9999.0)
    b = Budget(max_requests=1, deadline=0.1, used=1, started=1000.0)

    with pytest.raises(BudgetExceeded) as exc:
        b.take()
    assert "请求数预算耗尽" in str(exc.value)
    assert "时间预算" not in str(exc.value)


def test_meta_reports_used_and_elapsed_ms(monkeypatch) -> None:
    """meta() 的 elapsed_ms 取整毫秒；used 跟随 take 次数。"""
    _fake_time(monkeypatch, 1002.345)
    b = Budget(started=1000.0)
    assert b.elapsed == pytest.approx(2.345)
    assert b.meta() == {"requests": 0, "elapsed_ms": 2345}

    b.take()
    assert b.meta() == {"requests": 1, "elapsed_ms": 2345}


# ---------------------------------------------------------------------------
# FetchResult：list 子类兼容性与元数据
# ---------------------------------------------------------------------------


def test_fetch_result_behaves_like_list() -> None:
    """len / 索引 / 切片 / 迭代 / 相等比较 / 成员判断都必须与 list 一致。"""
    items = [{"id": 1}, {"id": 2}, {"id": 3}]
    res = FetchResult(items)

    assert isinstance(res, list)
    assert len(res) == 3
    assert res[0] == {"id": 1}
    assert res[-1] == {"id": 3}
    assert [x["id"] for x in res] == [1, 2, 3]
    assert res == items
    assert {"id": 2} in res
    assert list(res) == items


def test_fetch_result_json_dumps_matches_plain_list() -> None:
    """json.dumps 走 list 序列化路径，元数据字段不会漏进 JSON。"""
    items = [{"id": 1, "content": "正文"}]
    res = FetchResult(items, truncated=True, next_page=2, requests=3)

    assert json.dumps(res, ensure_ascii=False) == json.dumps(items, ensure_ascii=False)
    assert json.loads(json.dumps(res)) == items


def test_fetch_result_defaults_and_meta() -> None:
    """默认不截断、无下一页、零请求；meta() 原样回读三个字段。"""
    res = FetchResult([1, 2])

    assert res.truncated is False
    assert res.next_page is None
    assert res.requests == 0
    assert res.meta() == {"truncated": False, "next_page": None, "requests": 0}


def test_fetch_result_meta_reflects_arguments() -> None:
    """显式传入的分页元数据要能通过 meta() 与属性双通道读到。"""
    res = FetchResult([1], truncated=True, next_page=7, requests=40)

    assert res.truncated is True
    assert res.next_page == 7
    assert res.requests == 40
    assert res.meta() == {"truncated": True, "next_page": 7, "requests": 40}
    # 元数据不参与长度计算
    assert len(res) == 1
