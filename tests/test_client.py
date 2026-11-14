
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
from coolapk_mcp.client import CoolapkClient, CoolapkError, _looks_read_only
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


async def _noop_backoff(_attempt: int) -> None:
    """退避等待的空实现：用例不关心真实等待时长"""
    return None


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

    def test_looks_read_only_shapes(self):
        """只读形态判定：末段后缀命中/纯数字放行，层级不足与非 v6 一律拒"""
        assert _looks_read_only("/v6/feed/detail")
        assert _looks_read_only("/v6/feed/replyList")
        assert _looks_read_only("/v6/user/space")
        assert _looks_read_only("/v6/topic/newTagDetail")
        assert _looks_read_only("/v6/user/123")
        assert not _looks_read_only("/v6/feed/like")
        assert not _looks_read_only("/v6/feed/like/")
        assert not _looks_read_only("/v6/feed")
        assert not _looks_read_only("/v5/feed/detail")

    def test_strict_mode_allows_known_read_paths(self, monkeypatch):
        monkeypatch.setattr(CoolapkClient, "GET_URL_STRICT", True)
        for path in (
            "/v6/feed/detail?id=1&page=2",
            "/v6/feed/replyList?id=1",
            "/v6/user/space?uid=1",
            "/v6/search?q=x",
            "/v6/topic/newTagDetail?tag=x",
            "/v6/account/checkLoginInfo",
        ):
            assert CoolapkClient._check_get_url_path(path) == path

    def test_strict_mode_rejects_unknown_shape(self, monkeypatch):
        """开启严格模式后，形态不像只读端点的路径被挡（未知写端点也挡住）"""
        monkeypatch.setattr(CoolapkClient, "GET_URL_STRICT", True)
        for path in ("/v6/feed/obscureAction", "/v6/user/bindThing"):
            with pytest.raises(CoolapkError) as excinfo:
                CoolapkClient._check_get_url_path(path)
            assert excinfo.value.error_code == 403
            assert "严格模式" in excinfo.value.message

    def test_default_mode_keeps_escape_hatch(self, monkeypatch):
        """默认（非严格）仍放行上表未覆盖的只读接口，逃生舱不被收紧"""
        monkeypatch.setattr(CoolapkClient, "GET_URL_STRICT", False)
        assert CoolapkClient._check_get_url_path("/v6/feed/obscureAction") == "/v6/feed/obscureAction"


# ---------------- 403 / 429 细分：risk vs auth_expired、Retry-After ----------------

_RISK_BODY = {"message": "操作过于频繁，请稍后再试"}


class TestHttpStatusClassification:
    async def test_403_with_risk_hint_becomes_risk(self):
        """403 + 风控文案：归 risk，不要误导用户去重新登录"""
        client = _make_client(lambda request: httpx.Response(403, json=_RISK_BODY))

        with pytest.raises(CoolapkError) as excinfo:
            await client.get("/v6/feed/detail")

        err = excinfo.value
        assert err.kind == "risk"
        assert err.retryable is False
        # 服务端没给 Retry-After 时用保守默认值，而不是 None
        assert err.retry_after == 120.0

    async def test_403_risk_honours_retry_after_header(self):
        """403 + Retry-After：把服务端建议等待时间透出来"""
        client = _make_client(
            lambda request: httpx.Response(
                403, json=_RISK_BODY, headers={"Retry-After": "30"}
            )
        )

        with pytest.raises(CoolapkError) as excinfo:
            await client.get("/v6/feed/detail")

        assert excinfo.value.kind == "risk"
        assert excinfo.value.retry_after == 30.0

    async def test_403_without_hint_stays_auth_expired(self):
        """空响应体的 403 仍是登录态失效（原有语义不动）"""
        client = _make_client(lambda request: httpx.Response(403))

        with pytest.raises(CoolapkError) as excinfo:
            await client.get("/v6/feed/detail")

        assert excinfo.value.kind == "auth_expired"

    async def test_429_with_long_retry_after_stops_retrying(self, monkeypatch):
        """服务端要求久等（超过退避上限）时不再重试，避免加深封禁"""
        calls: list[httpx.Request] = []

        def handler(request):
            calls.append(request)
            return httpx.Response(429, headers={"Retry-After": "600"})

        client = _make_client(handler)

        with pytest.raises(CoolapkError) as excinfo:
            await client.get("/v6/feed/detail")

        err = excinfo.value
        assert err.kind == "rate_limit"
        assert err.retryable is True
        assert err.retry_after == 600.0
        assert len(calls) == 1

    async def test_429_without_header_is_retried(self, monkeypatch):
        """没有 Retry-After 的 429 仍按可重试处理，耗尽后保留 rate_limit 语义"""
        monkeypatch.setattr(throttle, "backoff_sleep", _noop_backoff)
        calls: list[httpx.Request] = []

        def handler(request):
            calls.append(request)
            return httpx.Response(429)

        client = _make_client(handler)

        with pytest.raises(CoolapkError) as excinfo:
            await client.get("/v6/feed/detail")

        assert excinfo.value.kind == "rate_limit"
        assert excinfo.value.retryable is True
        assert len(calls) == throttle.MAX_RETRIES + 1


class TestStatusHelpers:
    def test_retry_after_seconds_parses_number(self):
        resp = httpx.Response(429, headers={"Retry-After": "17"})
        assert client_mod._retry_after_seconds(resp) == 17.0

    def test_retry_after_seconds_parses_http_date(self):
        resp = httpx.Response(
            429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}
        )
        got = client_mod._retry_after_seconds(resp)
        # 历史时间点：已过期按 0 处理，但必须解析成功而不是 None
        assert got == 0.0

    def test_retry_after_seconds_missing_or_garbage(self):
        assert client_mod._retry_after_seconds(httpx.Response(429)) is None
        assert (
            client_mod._retry_after_seconds(
                httpx.Response(429, headers={"Retry-After": "soon"})
            )
            is None
        )

    def test_response_hint_extracts_message(self):
        resp = httpx.Response(403, json={"message": " 被限制 "})
        assert client_mod._response_hint(resp) == "被限制"

    def test_response_hint_ignores_html(self):
        resp = httpx.Response(403, text="<html><body>403</body></html>")
        assert client_mod._response_hint(resp) == ""


# ---------------- X-App-Token 短 TTL 缓存 ----------------


class TestTokenCache:
    async def test_reuses_token_within_ttl(self, monkeypatch):
        """TTL 内复用：连发两次请求只算一次 bcrypt（约 60ms/次）"""
        calls: list[str] = []

        def fake_token(device_code: str):
            calls.append(device_code)
            return f"tok-{len(calls)}", b"blob"

        monkeypatch.setattr(client_mod, "generate_token_v3", fake_token)
        client = _make_client(_recording_handler([]))

        await client.get("/v6/feed/detail", params={"id": 1})
        await client.get("/v6/feed/detail", params={"id": 2})

        assert calls == ["test-device-code"]

    async def test_ttl_zero_disables_cache(self, monkeypatch):
        """TOKEN_CACHE_TTL=0：排障模式，每次请求都重算"""
        calls: list[str] = []

        def fake_token(device_code: str):
            calls.append(device_code)
            return "tok", b"blob"

        monkeypatch.setattr(client_mod, "generate_token_v3", fake_token)
        monkeypatch.setattr(CoolapkClient, "TOKEN_CACHE_TTL", 0.0)
        client = _make_client(_recording_handler([]))

        await client.get("/v6/feed/detail", params={"id": 1})
        await client.get("/v6/feed/detail", params={"id": 2})

        assert len(calls) == 2


# ---------------- uid 索引延迟写盘 ----------------


class TestUidIndexDebounce:
    async def test_repeated_lists_collapse_into_one_write(self, monkeypatch):
        """合并窗口内的多次列表响应只落盘一次，且内容完整"""
        monkeypatch.setattr(CoolapkClient, "INDEX_SAVE_DELAY", 0.05)
        payload = [
            {"entityType": "feed", "id": 1, "uid": 2},
            {"entityType": "feed", "id": 3, "uid": 4},
        ]
        client = _make_client(_recording_handler([], payload=payload))

        writes: list[int] = []
        original = client._save_uid_index

        def counting() -> None:
            writes.append(1)
            original()

        monkeypatch.setattr(client, "_save_uid_index", counting)

        await client.get("/v6/feed/replyList", params={"id": 1})
        await client.get("/v6/feed/replyList", params={"id": 2})
        await asyncio.sleep(0.3)

        assert writes == [1]
        saved = json.loads(CoolapkClient.INDEX_FILE.read_text(encoding="utf-8"))
        assert saved == {"1": 2, "3": 4}

    async def test_close_flushes_pending_index(self, monkeypatch):
        """进程退出前 close() 必须把窗口内的脏数据刷掉"""
        monkeypatch.setattr(CoolapkClient, "INDEX_SAVE_DELAY", 30.0)
        payload = [{"entityType": "feed", "id": 9, "uid": 8}]
        client = _make_client(_recording_handler([], payload=payload))

        await client.get("/v6/feed/replyList", params={"id": 1})
        await client.close()

        saved = json.loads(CoolapkClient.INDEX_FILE.read_text(encoding="utf-8"))
        assert saved == {"9": 8}
