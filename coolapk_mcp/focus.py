"""评论聚焦：把「楼主回复 + 热门评论」的翻页/去重/预算逻辑收到一处。

server 的 coolapk_feed_detail 与 CLI 的 coolapk feed 此前各写一套，参数一改就
漂移；这里做唯一实现，两边只负责把结果塞进模型。
"""

from __future__ import annotations

import asyncio
from typing import Any

from coolapk_mcp.budget import Budget, BudgetExceeded
from coolapk_mcp.client import CoolapkError

# 每个来源（楼主回复 / 热门评论）最多翻的页数
FOCUS_MAX_PAGES = 5
# 焦点评论模式（default 表示不聚焦，走原始回复页）
FOCUS_MODES = ("default", "smart", "author", "hot", "all")


async def collect_focus(
    client: Any,
    feed_id: int,
    mode: str,
    *,
    max_pages: int = FOCUS_MAX_PAGES,
    deadline: float = 25.0,
    start_page: int = 1,
) -> tuple[list[dict], dict | None]:
    """按 mode 拉取焦点评论，返回 (去重后的评论列表, focus_meta)。

    两个来源共用一份请求预算：各最多 max_pages 页（某页不足 10 条即末页），
    另留 2 次余量，避免评论上千的帖子把一次工具调用拖进超时。
    翻页、跨页去重与截断判定都在 client 的分页方法里完成。

    mode 不在 smart/author/hot 内时返回 ([], None)，调用方按默认回复列表处理。

    start_page 用于消费上一次返回的 focus.next_page：两个来源都从这一页起翻，
    已经取过的页不会重复计费（末页不足 10 条会立刻停）。
    """
    mode = (mode or "default").strip().lower()
    if mode not in ("smart", "author", "hot"):
        return [], None

    start = max(1, int(start_page or 1))
    budget = Budget(max_requests=max_pages * 2 + 2, deadline=deadline)
    # 两路并发发起：global 限速模式下仍会被限速器串行化（行为与串行等价），
    # 只有 COOLAPK_RATE_LIMIT_MODE=bucket 时才真正并行压延迟。
    # Budget.take() 是同步自增，共享同一个 Budget 不需要额外加锁。
    jobs: list[tuple[str, Any]] = []
    if mode in ("smart", "author"):
        jobs.append(
            (
                "author",
                client.get_author_replies_paged(
                    feed_id, max_pages=max_pages, budget=budget, start_page=start
                ),
            )
        )
    if mode in ("smart", "hot"):
        jobs.append(
            (
                "hot",
                client.get_popular_replies_paged(
                    feed_id, max_pages=max_pages, budget=budget, start_page=start
                ),
            )
        )

    outcomes = await asyncio.gather(*(job for _, job in jobs), return_exceptions=True)
    notes: list[str] = []
    budget_hit = False
    fetched: dict[str, Any] = {}
    for (name, _), res in zip(jobs, outcomes, strict=True):
        if isinstance(res, BudgetExceeded):
            # 预算耗尽：已拿到的来源照常展示，没取的部分按截断处理
            budget_hit = True
            notes.append("请求预算耗尽，已返回部分评论")
        elif isinstance(res, CoolapkError):
            # 记住哪一路没取到：只丢热门评论时数据仍有多半，但绝不能标成「已取完」
            notes.append(f"{name} 来源拉取失败（{res}）")
        elif isinstance(res, BaseException):
            # 非预期异常照旧向上抛，由调用方（server/CLI）统一兜底
            raise res
        else:
            fetched[name] = res

    author_res = fetched.get("author")
    hot_res = fetched.get("hot")
    author_list: list[dict] = list(author_res or [])
    hot_list: list[dict] = list(hot_res or [])

    merged: list[dict] = []
    seen: set[str] = set()
    for item in author_list + hot_list:
        if not isinstance(item, dict):
            continue
        rid = item.get("id")
        if rid is None or rid == "":
            # 没有 id 就无法判重：直接保留，否则所有缺 id 的条目会共用 "None"
            # 键，只剩第一条
            merged.append(item)
            continue
        key = str(rid)
        if key not in seen:
            seen.add(key)
            merged.append(item)

    sources = [res for res in (author_res, hot_res) if res is not None]
    missing = [name for name, _ in jobs if name not in fetched]
    # 少了一整路来源也算截断：对「只剩楼主回复」的结果宣称已取完是在骗调用方
    truncated = (
        budget_hit
        or bool(missing)
        or any(bool(getattr(res, "truncated", False)) for res in sources)
    )
    next_pages = [
        p
        for p in (getattr(res, "next_page", None) for res in sources)
        if isinstance(p, int)
    ]
    pages_fetched = sum(int(getattr(res, "requests", 0) or 0) for res in sources)
    if not pages_fetched:
        # client 没上报请求数时回退到预算计数（两者语义都是"发出的页请求数"）
        pages_fetched = budget.used

    meta = {
        "mode": mode,
        "author_replies": len(author_list),
        "hot_replies": len(hot_list),
        "total_shown": len(merged),
        # 翻页统计：pages_fetched 为本次实际发出的页请求数合计；
        # truncated=True 表示撞到 max_pages 或请求/时间预算上限，后面还有评论没取；
        # next_page 是继续深入的起始页码（未截断时为 None）
        "pages_fetched": pages_fetched,
        "truncated": truncated,
        # 取 max 而不是 min：两路各自停在不同页时，从较后的页续抓不会重复
        # 拉取某一路已经翻完的页（重复页会被去重并按「整页重复」提前停）
        "next_page": (max(next_pages) if (truncated and next_pages) else (start if truncated else None)),
        "budget": budget.meta(),
    }
    if notes:
        meta["error"] = "；".join(notes)
    if missing:
        meta["missing_sources"] = missing
    return merged, meta
