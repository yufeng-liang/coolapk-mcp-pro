"""client.py 单元测试 — 全程不联网（httpx.MockTransport / monkeypatch）

覆盖本轮 P0 改动：
- _thumb_fetch_url：带 query 的缩略地址、无 query 与旧行为一致、非 jpg 返回 None
- 验证码负缓存：冷却期内 _request 直接抛 kind=captcha 且不发网络请求
- token 生成失败（缺 auth blob / 读盘失败）：转成 kind=config 的结构化错误
- httpx 底层异常（ReadError）：统一转 kind=network
- 限速后的二次缓存复查：并发等待后不再重复打服务器
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from coolapk_mcp import client as client_mod
from coolapk_mcp import throttle
from coolapk_mcp.budget import Budget, FetchResult
from coolapk_mcp.client import CoolapkClient, CoolapkError
from coolapk_mcp.config import AppConfig


def _make_client(handler) -> CoolapkClient:
    """构造「已登录 + 零限速间隔 + MockTransport」的客户端。

    用 AppConfig() 空构造（不读磁盘配置），登录态手工填上，避免依赖本机登录环境。
    """
    config = AppConfig()
    config.uid = "10086"
    config.token = "test-token"
    config.device_code = "test-device-code"
    client = CoolapkClient(config=config)
    # 用 MockTransport 顶掉真实网络连接
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    # 限速归零：用例不关心节流，避免每次请求白等 1.2s
    client._limiter = throttle.RateLimiter(min_interval=0.0)
    return client


def _recording_handler(calls: list[httpx.Request], payload=None):
    """记录请求并返回固定 JSON 的 handler"""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": [] if payload is None else payload})

    return handler


# ---------------- _thumb_fetch_url ----------------


class TestThumbFetchUrl:
    def test_query_is_kept_after_thumbnail_suffix(self):
        """带 query 的图：.m.jpg 必须加在 path 尾部，query 留在最后"""
        url = "https://image.coolapk.com/feed/2024/0329/abc@1080x1080.jpg?sign=xyz&t=1"
        assert CoolapkClient._thumb_fetch_url(url) == (
            "https://image.coolapk.com/feed/2024/0329/abc@1080x1080.jpg.m.jpg?sign=xyz&t=1"
        )

    def test_no_query_keeps_legacy_behaviour(self):
        """无 query 时输出与旧实现 url + '.m.jpg' 完全一致"""
        url = "https://image.coolapk.com/feed/2024/0329/abc@1080x1080.jpg"
        assert CoolapkClient._thumb_fetch_url(url) == url + ".m.jpg"

    def test_non_jpg_returns_none(self):
        assert (
            CoolapkClient._thumb_fetch_url("https://image.coolapk.com/a@1080x1080.png")
            is None
        )

    def test_jpg_without_size_suffix_returns_none(self):
        assert CoolapkClient._thumb_fetch_url("https://image.coolapk.com/a.jpg") is None

    def test_switch_off_returns_none(self, monkeypatch):
        monkeypatch.setenv("COOLAPK_IMG_THUMBNAIL", "0")
        assert (
            CoolapkClient._thumb_fetch_url("https://image.coolapk.com/a@1080x1080.jpg")
            is None
        )


# ---------------- 验证码负缓存 ----------------


def test_captcha_cooldown_raises_without_network():
    """冷却命中：抛 captcha（带 retry_after），且一个请求都不发"""
    calls: list[httpx.Request] = []
    client = _make_client(_recording_handler(calls))
    client._blocked["/v6/feed/detail"] = time.monotonic() + 120.0

    with pytest.raises(CoolapkError) as excinfo:
        asyncio.run(client.get("/v6/feed/detail?id=1&page=2"))

    err = excinfo.value
    assert err.kind == "captcha"
    assert err.retry_after is not None
    assert 0.0 < err.retry_after <= 120.0
    assert calls == []


def test_expired_cooldown_does_not_block():
    """冷却过期：_endpoint_blocked 清掉记录，请求正常发出"""
    calls: list[httpx.Request] = []
    client = _make_client(_recording_handler(calls))
    client._blocked["/v6/search"] = time.monotonic() - 1.0  # 已过期

    data = asyncio.run(client.get("/v6/search", {"type": "feed", "searchValue": "x"}))

    assert data == []
    assert len(calls) == 1
    assert "/v6/search" not in client._blocked


def test_captcha_unlock_endpoint_is_exempt():
    """解验证码入口即使被写入冷却也必须放行，否则风控后无法自助解锁"""
    calls: list[httpx.Request] = []
    client = _make_client(_recording_handler(calls, payload={}))
    client._blocked["/v6/account/requestValidate"] = time.monotonic() + 600.0

    result = asyncio.run(client.request_validate("1234"))

    assert result == {}
    assert len(calls) == 1


# ---------------- token 生成失败 -> config ----------------


def test_token_runtime_error_becomes_config(monkeypatch):
    """缺 auth_blob.bin / coolapk.apk 时 get_blob 抛 RuntimeError"""
    calls: list[httpx.Request] = []
    client = _make_client(_recording_handler(calls))

    def boom(device):
        raise RuntimeError("libauth blob 未缓存（token V3 签名必需）")

    monkeypatch.setattr(client_mod, "generate_token_v3", boom)

    with pytest.raises(CoolapkError) as excinfo:
        asyncio.run(client.get("/v6/user/space?uid=1"))

    err = excinfo.value
    assert err.kind == "config"
    assert "RuntimeError" in err.message
    assert "coolapk login" in err.message
    assert calls == []


def test_token_os_error_becomes_config(monkeypatch):
    """读盘失败（OSError）同样归入 config"""
    calls: list[httpx.Request] = []
    client = _make_client(_recording_handler(calls))

    def boom(device):
        raise OSError("[Errno 13] Permission denied: auth_blob.bin")

    monkeypatch.setattr(client_mod, "generate_token_v3", boom)

    with pytest.raises(CoolapkError) as excinfo:
        asyncio.run(client.get("/v6/user/profile?uid=1"))

    err = excinfo.value
    assert err.kind == "config"
    assert "OSError" in err.message
    assert calls == []


# ---------------- httpx 底层异常 -> network ----------------


def test_read_error_becomes_network(monkeypatch):
    """ReadError 属于 httpx.HTTPError 兜底分支：转 network 并按限速重试"""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadError("connection reset by peer", request=request)

    client = _make_client(handler)

    async def _no_sleep(attempt):
        return None

    # 退避等待对断言无意义，置空保持用例秒级
    monkeypatch.setattr(throttle, "backoff_sleep", _no_sleep)

    with pytest.raises(CoolapkError) as excinfo:
        asyncio.run(client.get("/v6/feed/replyList?id=1&page=1"))

    err = excinfo.value
    assert err.kind == "network"
    assert "ReadError" in err.message
    assert len(calls) == throttle.MAX_RETRIES + 1


# ---------------- 限速后的二次缓存复查 ----------------


class _SecondCheckHitCache(throttle.TTLCache):
    """首查必定 miss、次查必定 hit —— 用来证明限速等待后确实又查了一次缓存"""

    def __init__(self, payload):
        super().__init__()
        self.payload = payload
        self.get_calls = 0

    def get(self, key):
        self.get_calls += 1
        return self.payload if self.get_calls >= 2 else None


@pytest.mark.skipif(not throttle.CACHE_ENABLED, reason="环境变量禁用了缓存")
def test_cache_is_rechecked_after_rate_limit_wait():
    calls: list[httpx.Request] = []
    client = _make_client(_recording_handler(calls))
    fake = _SecondCheckHitCache({"id": "cached-value"})
    client._cache = fake

    data = asyncio.run(client.get("/v6/user/profile", {"uid": 1}))

    assert data == {"id": "cached-value"}
    assert fake.get_calls == 2  # 首查 + 限速后复查
    assert calls == []  # 复查命中，没有真正发请求

# ---------------- 分页预算 / FetchResult ----------------


def _reply_pages(sizes: dict[int, int], requested: list[int]):
    """伪 get_feed_replies：按页码返回指定条数，并记录实际请求过的页"""

    async def fake_replies(feed_id, page=1, list_type="lastupdate_desc"):
        requested.append(page)
        return [{"id": f"{page}-{i}"} for i in range(sizes.get(page, 0))]

    return fake_replies


def test_budget_limits_pages_and_marks_truncated(monkeypatch):
    """预算只有 2 次时只翻 2 页，返回 FetchResult 且标 truncated/next_page"""
    client = _make_client(_recording_handler([]))
    requested: list[int] = []
    # 每页都满 10 条：说明后面还有，必须靠预算刹住
    monkeypatch.setattr(client, "get_feed_replies", _reply_pages({1: 10, 2: 10, 3: 10}, requested))

    result = asyncio.run(
        client.get_all_replies(123, max_pages=5, budget=Budget(max_requests=2))
    )

    assert isinstance(result, FetchResult)
    assert requested == [1, 2]  # 第 3 页的 take() 就被预算拦下
    assert len(result) == 20
    assert result.truncated is True
    assert result.next_page == 3
    assert result.requests == 2
    assert result.meta() == {"truncated": True, "next_page": 3, "requests": 2}


def test_short_last_page_is_not_truncated(monkeypatch):
    """第 2 页只有 3 条 = 到底了，不该标截断"""
    client = _make_client(_recording_handler([]))
    requested: list[int] = []
    monkeypatch.setattr(client, "get_feed_replies", _reply_pages({1: 10, 2: 3}, requested))

    result = asyncio.run(client.get_all_replies(123, max_pages=5))

    assert requested == [1, 2]
    assert len(result) == 13
    assert result.truncated is False
    assert result.next_page is None
    assert result.requests == 2


def test_fetch_result_is_still_a_list():
    """FetchResult 对上层仍是 list：len/索引/json 序列化/直接迭代都照常"""
    items = [{"id": 1}, {"id": 2}]
    result = FetchResult(items, truncated=True, next_page=3, requests=7)

    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0] == {"id": 1}
    assert result[-1] == {"id": 2}
    assert [item["id"] for item in result] == [1, 2]
    assert json.dumps(result) == json.dumps(items)
    assert result.meta() == {"truncated": True, "next_page": 3, "requests": 7}


# ---------------- 单飞（single-flight） ----------------


def test_concurrent_same_get_uses_single_flight():
    """同 URL 的两个并发 get 只发一次网络请求，两个结果相等"""
    calls: list[httpx.Request] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"data": {"id": "feed-1"}})

    client = _make_client(handler)

    async def scenario():
        first = asyncio.create_task(client.get("/v6/feed/detail?id=1"))
        await entered.wait()  # 第一个请求已经挂在网络层
        second = asyncio.create_task(client.get("/v6/feed/detail?id=1"))
        # 让第二个任务跑到单飞登记点：应挂在同一 Future 上而不是再发一次
        for _ in range(20):
            await asyncio.sleep(0.005)
            if len(client._inflight) == 1 and not second.done():
                break
        assert len(calls) == 1
        assert not second.done()
        release.set()
        return await asyncio.gather(first, second)

    a, b = asyncio.run(scenario())

    assert a == b == {"id": "feed-1"}
    assert len(calls) == 1  # 全程只有一次真实请求


# ---------------- 缓存键身份 ----------------


def test_cache_key_carries_identity():
    """缓存键必须带 uid + token 短哈希，否则跨账号会串缓存"""
    client = _make_client(_recording_handler([]))
    params = {"id": 1, "page": 1}
    key = client._cache_key("/v6/feed/replyList", params)

    assert key.startswith("10086:")
    # 参数顺序不影响键
    assert key == client._cache_key("/v6/feed/replyList", {"page": 1, "id": 1})

    # 换账号 -> 键不同
    other_uid = _make_client(_recording_handler([]))
    other_uid.config.uid = "20000"
    assert key != other_uid._cache_key("/v6/feed/replyList", params)

    # 同账号换 token -> 键不同
    other_token = _make_client(_recording_handler([]))
    other_token.config.token = "another-token"
    assert key != other_token._cache_key("/v6/feed/replyList", params)

    # 未登录（token 为空）也要有稳定键，且 None/{} 等价
    anonymous = _make_client(_recording_handler([]))
    anonymous.config.token = ""
    assert anonymous._cache_key("/v6/x", None) == anonymous._cache_key("/v6/x", {})
    assert anonymous._cache_key("/v6/x", None) != key


# ---------------- get_url 路径守卫 ----------------


class TestGetUrlPathGuard:
    def test_normal_read_path_passes(self):
        path = "/v6/feed/detail?id=1&page=2"
        assert CoolapkClient._check_get_url_path(path) == path

    @pytest.mark.parametrize(
        "path",
        [
            "/v6/feed/./like",
            "/v6/feed/../like",
            "/v6/feed/%2e%2e/like",
            "/v6/feed\\like",
            "/v6/feed/%5clike",
            "/v6/feed/like;foo",
            "/v6/feed/like%3bfoo",
            "/v6/feed/like%00",
        ],
    )
    def test_dot_segments_and_backslash_rejected(self, path):
        """点段会被反代归一化成写端点，反斜杠可能被上游当分隔符"""
        with pytest.raises(CoolapkError) as excinfo:
            CoolapkClient._check_get_url_path(path)
        assert excinfo.value.error_code == 403

