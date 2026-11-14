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


@pytest.fixture()
def fake_tick_sleep(monkeypatch):
    """把限速器的等待出口换成记录器：断言"该睡多久"，不赌 runner 的墙钟"""
    sleeps: list[float] = []

    async def fake(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(throttle, "_sleep", fake)
    return sleeps


@pytest.mark.asyncio
async def test_rate_limiter_enforces_min_interval(fake_tick_sleep):
    """首次立即放行，第二次（刚刚过）要等满 min_interval"""
    limiter = throttle.RateLimiter(min_interval=0.05)

    await limiter.acquire()
    assert fake_tick_sleep == []          # 冷启动 _last=0，无等待

    limiter._last = throttle.time.monotonic()   # 人为把"上次放行"钉在现在
    await limiter.acquire()

    assert len(fake_tick_sleep) == 1
    # wait = _last + min_interval - now，只可能 ≤ min_interval 且接近它
    assert 0.04 < fake_tick_sleep[0] <= 0.05


@pytest.mark.asyncio
async def test_rate_limiter_zero_interval_passes_immediately(fake_tick_sleep):
    """min_interval=0 时不等待（关闭限速的开发/测试路径）"""
    limiter = throttle.RateLimiter(min_interval=0)

    for _ in range(3):
        await limiter.acquire()

    assert fake_tick_sleep == []


@pytest.mark.asyncio
async def test_rate_limiter_serializes_concurrent_callers(fake_tick_sleep):
    """并发调用串行化：3 个协程里有 2 个要各自等一个 interval"""
    limiter = throttle.RateLimiter(min_interval=0.02)

    await asyncio.gather(*(limiter.acquire() for _ in range(3)))

    assert len(fake_tick_sleep) == 2
    assert all(0.015 < delay <= 0.02 for delay in fake_tick_sleep)


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


@pytest.mark.asyncio
async def test_backoff_sleep_honours_retry_after_floor(fake_sleep, monkeypatch):
    """服务端 Retry-After 大于退避曲线时，按 Retry-After 睡够"""
    monkeypatch.setattr(throttle.random, "uniform", lambda a, b: 0.0)

    await throttle.backoff_sleep(0, floor=5.0)

    assert fake_sleep == [5.0]


@pytest.mark.asyncio
async def test_backoff_sleep_floor_is_capped(fake_sleep, monkeypatch):
    """Retry-After 再大也不能突破 COOLAPK_MAX_BACKOFF"""
    monkeypatch.setattr(throttle.random, "uniform", lambda a, b: 0.0)
    monkeypatch.setattr(throttle, "MAX_BACKOFF", 3.0)

    await throttle.backoff_sleep(0, floor=99.0)

    assert fake_sleep == [3.0]


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
