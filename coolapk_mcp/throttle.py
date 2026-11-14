"""请求节制 — 限速、TTL 缓存、瞬态重试

目标是让客户端表现得像一个克制的人类用户，从源头降低触发风控的概率：
- 相邻 API 请求强制间隔（默认 1.2s，可用 COOLAPK_MIN_INTERVAL 覆盖）
- GET 成功响应按端点 TTL 缓存，重复查询不再打到服务器
- 仅对瞬态错误（超时/连接失败/5xx/429）做指数退避重试
- 验证码类错误不重试，改为负缓存，短期内绕开该端点

设 COOLAPK_NO_CACHE=1 可禁用缓存（调试用）。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from collections import OrderedDict


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


MIN_INTERVAL = _env_float("COOLAPK_MIN_INTERVAL", 1.2)
try:
    # 上限钉 8：backoff 0.8*(2**attempt)，重试再多也无意义且会指数膨胀等待
    MAX_RETRIES = min(8, max(0, int(_env_float("COOLAPK_MAX_RETRIES", 2))))
except (OverflowError, ValueError):
    MAX_RETRIES = 2
CACHE_ENABLED = os.environ.get("COOLAPK_NO_CACHE", "") not in ("1", "true", "True")
# 限速模式：global = 全局串行（默认，最保守，行为与历史版本一致）；
# bucket = 按端点资源分桶各自计时，不同端点互不阻塞，配合并发可缩短一次工具调用的总耗时
RATE_LIMIT_MODE = (os.environ.get("COOLAPK_RATE_LIMIT_MODE") or "global").strip().lower()
# 单次退避等待上限（秒）：重试次数配置得很大时，2**attempt 会膨胀到上百秒，钉住上限
MAX_BACKOFF = max(0.0, _env_float("COOLAPK_MAX_BACKOFF", 30.0))
# 缓存体积上限（字节）：只按条数限流时，几条超长评论列表就能把常驻内存顶到几十 MB
CACHE_MAX_BYTES = int(_env_float("COOLAPK_CACHE_MAX_BYTES", 8 * 1024 * 1024))

# 各端点缓存时长（秒）：内容越稳定 TTL 越长
_TTL_BY_PREFIX: list[tuple[str, float]] = [
    ("/v6/feed/detail", 600),
    ("/v6/feed/replyList", 600),
    ("/v6/feed/hotReplyList", 600),
    ("/v6/user/space", 600),
    ("/v6/user/profile", 600),
    ("/v6/topic/newTagDetail", 600),
    ("/v6/search", 300),
]
_DEFAULT_TTL = 120.0


def ttl_for(path: str) -> float:
    for prefix, ttl in _TTL_BY_PREFIX:
        if path.startswith(prefix):
            return ttl
    return _DEFAULT_TTL


def bucket_for(path: str) -> str:
    """取 /v6/<资源>/... 的资源段作为限速桶名，取不到时归入 default

    只按第一段资源分桶：feed / user / topic / search 各自计时，
    同一资源下的不同子端点共享一个桶，避免并发时把同一资源打成并发。
    """
    parts = [p for p in path.split("?", 1)[0].strip("/").split("/") if p]
    if len(parts) >= 2 and parts[0] == "v6":
        return parts[1]
    return "default"


async def _sleep(delay: float) -> None:
    """限速等待的唯一出口：测试里替换成假时钟，断言"该睡多久"而不是"墙钟过了多久"。"""
    await asyncio.sleep(delay)


class RateLimiter:
    """限速器：默认全局串行；bucket 模式下按桶各自计时

    - global（默认）：无论并发多少调用，任意两次请求间隔不小于 min_interval，
      行为与历史版本完全一致，最保守。
    - bucket：每个桶独立计时，跨桶可并行；桶内仍严格串行。
      代价是瞬时并发数上升（最多等于桶数），仅在明确要压延迟时开启。
    """

    def __init__(self, min_interval: float = MIN_INTERVAL, mode: str | None = None):
        self.min_interval = max(0.0, min_interval)
        self.mode = (mode or RATE_LIMIT_MODE)
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._bucket_locks: dict[str, asyncio.Lock] = {}
        self._bucket_last: dict[str, float] = {}

    async def acquire(self, bucket: str = "") -> None:
        if self.mode != "bucket":
            async with self._lock:
                wait = self._last + self.min_interval - time.monotonic()
                if wait > 0:
                    await _sleep(wait)
                self._last = time.monotonic()
            return
        key = bucket or "default"
        lock = self._bucket_locks.setdefault(key, asyncio.Lock())
        async with lock:
            wait = self._bucket_last.get(key, 0.0) + self.min_interval - time.monotonic()
            if wait > 0:
                await _sleep(wait)
            self._bucket_last[key] = time.monotonic()


def _sizeof(value: object) -> int:
    """估算缓存条目的字节数（按 JSON 序列化长度），失败按 0 处理不阻塞缓存"""
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


class TTLCache:
    """LRU + TTL 缓存，只存 GET 的成功响应

    双重上限：条数（maxsize）与总字节（max_bytes）。只按条数算的话，
    一次 all_replies 翻 30 页的评论树就有几百 KB，几十条就能顶到几十 MB；
    超限时按 LRU 顺序淘汰，并累计 evictions 便于观测缓存是否过小。
    """

    def __init__(self, maxsize: int = 256, max_bytes: int | None = None):
        self.maxsize = maxsize
        self.max_bytes = CACHE_MAX_BYTES if max_bytes is None else max(0, max_bytes)
        self._store: OrderedDict[str, tuple[float, object, int]] = OrderedDict()
        self._bytes = 0
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def get(self, key: str) -> object | None:
        hit = self._store.get(key)
        if hit is None:
            self.misses += 1
            return None
        expires, value, size = hit
        if time.monotonic() > expires:
            del self._store[key]
            self._bytes -= size
            self.misses += 1
            return None
        self._store.move_to_end(key)
        self.hits += 1
        return value

    def put(self, key: str, value: object, ttl: float) -> None:
        size = _sizeof(value)
        old = self._store.pop(key, None)
        if old is not None:
            self._bytes -= old[2]
        self._store[key] = (time.monotonic() + ttl, value, size)
        self._bytes += size
        while self._store and (
            len(self._store) > self.maxsize
            or (self.max_bytes and self._bytes > self.max_bytes and len(self._store) > 1)
        ):
            _, (_, _, dropped) = self._store.popitem(last=False)
            self._bytes -= dropped
            self.evictions += 1

    def stats(self) -> dict:
        return {
            "entries": len(self._store),
            "bytes": self._bytes,
            "max_entries": self.maxsize,
            "max_bytes": self.max_bytes,
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
        }


async def backoff_sleep(attempt: int, floor: float = 0.0) -> None:
    """指数退避 + 抖动：0.8s, 1.6s, ...，单次不超过 MAX_BACKOFF。

    floor 是调用方要求的最小等待（例如服务端 429 的 Retry-After）：
    退避曲线追不上服务端要求的等待时间时，按服务端要求睡够，否则重试等于
    白撞一次限流窗口，反而加深封禁。最终值仍受 MAX_BACKOFF 约束。
    """
    delay = 0.8 * (2 ** attempt) + random.uniform(0, 0.4)
    if floor > delay:
        delay = floor
    if MAX_BACKOFF:
        delay = min(delay, MAX_BACKOFF)
    await asyncio.sleep(delay)
