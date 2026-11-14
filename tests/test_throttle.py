"""throttle 单元测试

覆盖：TTLCache 命中/过期/LRU 淘汰、RateLimiter 最小间隔、backoff_sleep 随 attempt 增长。
时间相关用例用假时钟（替换 throttle 模块里的 time 引用），不真睡；
只有 RateLimiter 用 20~50ms 的真实小间隔，整文件远低于 2s。
"""

from __future__ import annotations

import asyncio
import time
import types

import pytest

from coolapk_mcp import throttle


class _FakeClock:
    """可控单调时钟，替换 throttle.time 用（不污染全局 time 模块）"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock(monkeypatch):
    fake = _FakeClock()
    monkeypatch.setattr(throttle, "time", fake)
    return fake


# ---------------------------------------------------------------- TTLCache


def test_ttlcache_hit_returns_value(clock):
    """写入后命中，返回值一致"""
    cache = throttle.TTLCache(maxsize=4)
    cache.put("k", {"v": 1}, ttl=10)

    assert cache.get("k") == {"v": 1}
    assert cache.get("missing") is None


def test_ttlcache_expires_after_ttl(clock):
    """超过 TTL 后过期返回 None，并清理该键"""
    cache = throttle.TTLCache(maxsize=4)
    cache.put("k", "v", ttl=5)

    clock.advance(5.1)
    assert cache.get("k") is None
    # 过期键已被删除，内部存储不再持有它
    assert "k" not in cache._store


def test_ttlcache_lru_eviction(clock):
    """超过 maxsize 淘汰最久未使用的键；命中会把键移到最近使用端"""
    cache = throttle.TTLCache(maxsize=2)
    cache.put("a", 1, ttl=60)
    cache.put("b", 2, ttl=60)

    # 命中 a 后，a 变成最近使用，b 变成最旧
    assert cache.get("a") == 1
    cache.put("c", 3, ttl=60)

    assert cache.get("b") is None      # 被淘汰
    assert cache.get("a") == 1
    assert cache.get("c") == 3


def test_ttl_for_endpoint_prefix(clock):
    """端点 TTL：已知前缀取表内值，未知路径用默认值"""
    assert throttle.ttl_for("/v6/search?keyword=x") == 300
    assert throttle.ttl_for("/v6/feed/detail?id=1") == 600
    assert throttle.ttl_for("/v6/unknown") == throttle._DEFAULT_TTL


# ------------------------------------------------------------ RateLimiter


@pytest.mark.asyncio
async def test_rate_limiter_enforces_min_interval():
    """两次 acquire 的实际间隔不小于 min_interval"""
    limiter = throttle.RateLimiter(min_interval=0.05)

    start = time.perf_counter()
    await limiter.acquire()
    first = time.perf_counter() - start
    await limiter.acquire()
    second = time.perf_counter() - start

    assert first < 0.05          # 首次立即放行
    assert second - first >= 0.045  # 第二次至少等满间隔（留 5ms 计时误差余量）


@pytest.mark.asyncio
async def test_rate_limiter_zero_interval_passes_immediately():
    """min_interval=0 时不等待（关闭限速的开发/测试路径）"""
    limiter = throttle.RateLimiter(min_interval=0)

    start = time.perf_counter()
    for _ in range(3):
        await limiter.acquire()

    assert time.perf_counter() - start < 0.05


@pytest.mark.asyncio
async def test_rate_limiter_serializes_concurrent_callers():
    """并发调用串行化：3 个协程总计至少 2 * min_interval"""
    limiter = throttle.RateLimiter(min_interval=0.02)

    start = time.perf_counter()
    await asyncio.gather(*(limiter.acquire() for _ in range(3)))

    assert time.perf_counter() - start >= 0.035


# --------------------------------------------------------------- backoff


@pytest.fixture()
def fake_sleep(monkeypatch):
    """把 throttle.asyncio.sleep 换成记录器，不真等"""
    delays: list[float] = []

    async def _sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(throttle, "asyncio", types.SimpleNamespace(sleep=_sleep))
    return delays


@pytest.mark.asyncio
async def test_backoff_sleep_grows_exponentially(fake_sleep, monkeypatch):
    """抖动固定为 0 时，等待时间按 0.8 * 2**attempt 递增"""
    monkeypatch.setattr(throttle.random, "uniform", lambda a, b: 0.0)

    for attempt in range(4):
        await throttle.backoff_sleep(attempt)

    assert fake_sleep == [0.8, 1.6, 3.2, 6.4]


@pytest.mark.asyncio
async def test_backoff_sleep_respects_max_backoff(fake_sleep, monkeypatch):
    """重试次数配置很大时，单次退避也要被 COOLAPK_MAX_BACKOFF 钉住"""
    monkeypatch.setattr(throttle.random, "uniform", lambda a, b: 0.0)
    monkeypatch.setattr(throttle, "MAX_BACKOFF", 3.0)

    for attempt in range(6):
        await throttle.backoff_sleep(attempt)

    assert fake_sleep == [0.8, 1.6, 3.0, 3.0, 3.0, 3.0]


# ------------------------------------------------------- 限速分桶（bucket 模式）


class TestBucketFor:
    def test_resource_segment(self):
        """取 /v6/<资源>/... 的资源段作为桶名"""
        assert throttle.bucket_for("/v6/feed/detail?id=1") == "feed"
        assert throttle.bucket_for("/v6/user/space") == "user"
        assert throttle.bucket_for("/v6/search?type=feed&q=x") == "search"

    def test_fallback_to_default(self):
        """非 /v6/资源/... 形态一律归入 default 桶"""
        assert throttle.bucket_for("") == "default"
        assert throttle.bucket_for("/v6") == "default"
        assert throttle.bucket_for("/health") == "default"


@pytest.mark.asyncio
async def test_bucket_mode_does_not_block_across_buckets():
    """bucket 模式：不同桶各自计时，跨桶不互相等待"""
    limiter = throttle.RateLimiter(min_interval=0.05, mode="bucket")

    start = time.perf_counter()
    await asyncio.gather(limiter.acquire("feed"), limiter.acquire("user"))

    assert time.perf_counter() - start < 0.045


@pytest.mark.asyncio
async def test_bucket_mode_serializes_same_bucket():
    """bucket 模式：同一个桶内仍然严格串行"""
    limiter = throttle.RateLimiter(min_interval=0.05, mode="bucket")

    start = time.perf_counter()
    await asyncio.gather(limiter.acquire("feed"), limiter.acquire("feed"))

    assert time.perf_counter() - start >= 0.045


@pytest.mark.asyncio
async def test_global_mode_ignores_bucket_argument():
    """默认 global 模式：即使带桶名也全局串行（与历史行为一致）"""
    limiter = throttle.RateLimiter(min_interval=0.05, mode="global")

    start = time.perf_counter()
    await asyncio.gather(limiter.acquire("feed"), limiter.acquire("user"))

    assert time.perf_counter() - start >= 0.045


@pytest.mark.asyncio
async def test_backoff_sleep_jitter_within_bounds(fake_sleep):
    """真实抖动落在 [base, base+0.4) 区间内，且严格随 attempt 增长"""
    for attempt in range(3):
        await throttle.backoff_sleep(attempt)

    for attempt, delay in enumerate(fake_sleep):
        base = 0.8 * (2**attempt)
        assert base <= delay < base + 0.4
    assert fake_sleep[0] < fake_sleep[1] < fake_sleep[2]
