"""请求预算与分页结果。

把"翻多少页/发多少请求"从各处的魔法数字变成每次工具调用可控制的资源配额：
- Budget 由工具层创建，传入 client 的分页方法；
- 预算耗尽抛 BudgetExceeded，调用方返回已取部分并标记 truncated；
- FetchResult 是 list 的子类，旧的 len()/iter()/json 序列化行为保持不变，
  额外携带 truncated / next_page / requests 三个元数据字段。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


class BudgetExceeded(Exception):
    """请求数或时间预算耗尽。调用方应保留已取到的数据并标记 truncated。"""


@dataclass
class Budget:
    """一次工具调用的请求预算。默认值与当前 1.2s 限速下的安全上限对齐。"""

    max_requests: int = 40
    deadline: float = 20.0
    used: int = 0
    started: float = field(default_factory=time.monotonic)

    def take(self) -> None:
        """在发起一次上游请求前调用；超限即抛 BudgetExceeded。"""
        if self.used >= self.max_requests:
            raise BudgetExceeded(f"请求数预算耗尽（上限 {self.max_requests} 次）")
        if time.monotonic() - self.started >= self.deadline:
            raise BudgetExceeded(f"时间预算耗尽（上限 {self.deadline:.0f}s）")
        self.used += 1

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def meta(self) -> dict:
        return {"requests": self.used, "elapsed_ms": int(self.elapsed * 1000)}


class FetchResult(list):
    """list 子类，兼容旧调用方，同时携带分页元数据。"""

    def __init__(
        self,
        items=(),
        *,
        truncated: bool = False,
        next_page: int | None = None,
        requests: int = 0,
    ):
        super().__init__(items)
        self.truncated = truncated
        self.next_page = next_page
        self.requests = requests

    def meta(self) -> dict:
        return {
            "truncated": self.truncated,
            "next_page": self.next_page,
            "requests": self.requests,
        }
