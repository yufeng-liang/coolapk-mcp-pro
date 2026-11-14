"""B 组（性能 / 可观测性）回归：缓存字节上限、请求指标、匿名只读、摘要并发、/status、焦点预拉。"""

import asyncio
import json

import httpx
import pytest

from coolapk_mcp import __version__
from coolapk_mcp import server, throttle
from coolapk_mcp.budget import FetchResult
from coolapk_mcp.client import CoolapkClient, CoolapkError
from coolapk_mcp.config import AppConfig


def _feed_data(replynum: int = 7) -> dict:
    return {
        "id": 1001,
        "message": "<p>工具帖正文</p>",
        "userInfo": {"username": "楼主", "uid": 9},
        "replynum": replynum,
    }


def _reply(rid: int) -> dict:
    return {
        "id": rid,
        "message": f"<p>评论{rid}</p>",
        "userInfo": {"username": f"用户{rid}", "uid": rid},
    }


def _patch_client(monkeypatch: pytest.MonkeyPatch, fake: object) -> None:
    async def _fake_get_client():
        return fake

    monkeypatch.setattr(server, "_get_client", _fake_get_client)


# ---- B1 缓存双上限 ----


def test_ttlcache_evicts_by_bytes() -> None:
    """条数没超但字节超限时，也要按 LRU 淘汰，并累计 evictions / 命中统计。"""
    cache = throttle.TTLCache(maxsize=10, max_bytes=200)
    cache.put("a", {"x": "y" * 120}, 60)
    cache.put("b", {"x": "y" * 120}, 60)

    assert cache.get("a") is None, "最旧的条目要因字节上限被淘汰"
    assert cache.get("b") is not None
    stats = cache.stats()
    assert stats["entries"] == 1
    assert stats["bytes"] <= 200
    assert stats["evictions"] == 1
    assert (stats["hits"], stats["misses"]) == (1, 1)


def test_ttlcache_keeps_single_oversized_entry() -> None:
    """只剩一条时不再淘汰：否则超长响应永远进不了缓存，请求次数直接翻倍。"""
    cache = throttle.TTLCache(maxsize=4, max_bytes=50)
    cache.put("big", {"x": "y" * 100}, 60)

    assert cache.get("big") is not None
    assert cache.stats()["entries"] == 1


def test_ttlcache_expired_entry_releases_bytes() -> None:
    cache = throttle.TTLCache(maxsize=4, max_bytes=1000)
    cache.put("k", {"x": 1}, -1)

    assert cache.get("k") is None
    assert cache.stats()["bytes"] == 0


# ---- B4 请求指标 ----


def _make_client(handler) -> CoolapkClient:
    cfg = AppConfig()
    cfg.uid = "123"
    cfg.token = "tok"
    client = CoolapkClient(cfg)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def test_metrics_count_requests_and_cache_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(throttle, "CACHE_ENABLED", True)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"data": {"ok": 1}})

    client = _make_client(handler)

    async def run():
        await client.get("/v6/feed/detail?id=1")
        await client.get("/v6/feed/detail?id=1")
        metrics, cache = client.metrics(), client.cache_stats()
        await client.close()
        return metrics, cache

    metrics, cache = asyncio.run(run())

    assert calls == ["/v6/feed/detail"], "第二次必须命中缓存"
    assert metrics["requests"] == 1
    assert (metrics["cache_hits"], metrics["cache_misses"]) == (1, 1)
    assert metrics["errors"] == {}
    assert cache["enabled"] is True
    assert cache["entries"] == 1


def test_metrics_records_error_kinds() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    client = _make_client(handler)

    async def run():
        with pytest.raises(CoolapkError):
            await client.get("/v6/feed/detail?id=2")
        metrics = client.metrics()
        await client.close()
        return metrics

    metrics = asyncio.run(run())
    assert metrics["errors"].get("transient"), "5xx 应记到 transient 名下"


# ---- B5 匿名只读 ----


def test_anonymous_mode_skips_login_precheck(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COOLAPK_ALLOW_ANONYMOUS", "1")
    client = CoolapkClient(AppConfig())  # 未登录

    async def run():
        client._check_local_auth()  # 匿名放行，不抛
        headers = await client._build_headers()
        await client.close()
        return headers

    headers = asyncio.run(run())

    assert client.allow_anonymous is True
    assert "X-App-Token" not in headers, "匿名请求不能带需要登录态才能生成的 token"
    assert "Cookie" not in headers


def test_anonymous_off_blocks_guest() -> None:
    client = CoolapkClient(AppConfig())
    try:
        with pytest.raises(CoolapkError) as excinfo:
            client._check_local_auth()
        assert excinfo.value.kind == "auth_expired"
    finally:
        asyncio.run(client.close())


# ---- B2 author_digest 并发 ----


class _ConcurrentDigest:
    """feeds 会等 user_space 先开跑：串行实现下这个等待必然超时。"""

    def __init__(self) -> None:
        self.space_started = asyncio.Event()
        self.overlapped = False
        self.space_error: Exception | None = None

    async def get_user_feeds_pages(
        self, uid, pages=2, start_page=1, feed_type="feed", budget=None
    ):
        budget.take()
        try:
            await asyncio.wait_for(self.space_started.wait(), timeout=1.0)
            self.overlapped = True
        except asyncio.TimeoutError:
            pass
        return FetchResult(
            [{"id": 31, "message": "<p>帖子A</p>", "userInfo": {"username": "作者", "uid": uid}}],
            requests=1,
        )

    async def get_user_space(self, uid):
        self.space_started.set()
        if self.space_error is not None:
            raise self.space_error
        return {"uid": uid, "username": "作者"}


def test_author_digest_runs_feeds_and_profile_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ConcurrentDigest()
    _patch_client(monkeypatch, fake)

    payload = json.loads(asyncio.run(server.coolapk_author_digest(7)))

    assert fake.overlapped, "两个请求必须同时在途，否则仍是串行"
    assert payload["author"]["username"] == "作者"
    assert [p["id"] for p in payload["posts"]] == [31]


def test_author_digest_degrades_when_profile_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ConcurrentDigest()
    fake.space_error = CoolapkError("资料页 403", kind="risk")
    _patch_client(monkeypatch, fake)

    payload = json.loads(asyncio.run(server.coolapk_author_digest(7)))

    assert [p["id"] for p in payload["posts"]] == [31], "资料页失败不能吞掉帖子列表"
    assert "作者" not in json.dumps(payload["author"], ensure_ascii=False)
    assert payload["author_error"].startswith("CoolapkError:")


# ---- B3 /status ----


class _StatusClient:
    def cache_stats(self) -> dict:
        return {"entries": 2, "hits": 5, "bytes": 1024}

    def metrics(self) -> dict:
        return {"uptime_s": 12.0, "requests": 3, "errors": {"network": 1}}


def test_status_payload_shape() -> None:
    payload = asyncio.run(server._status_payload(_StatusClient()))

    assert payload["status"] == "ok"
    assert payload["version"] == __version__
    assert payload["uptime_s"] >= 0
    assert payload["login"]["logged_in"] is False
    assert payload["rate_limit"]["mode"] == throttle.RATE_LIMIT_MODE
    assert payload["rate_limit"]["min_interval_s"] == throttle.MIN_INTERVAL
    assert payload["cache"] == {"entries": 2, "hits": 5, "bytes": 1024}
    assert payload["requests"]["requests"] == 3
    assert payload["image"]["max_bytes"] == server.MAX_IMAGE_BYTES


def _asgi_call(app, path: str, headers: list[tuple[bytes, bytes]] | None = None):
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers or [],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
    }
    messages: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    async def run():
        await app(scope, receive, send)

    asyncio.run(run())
    start = next(m for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return start["status"], json.loads(body or b"{}")


def test_status_route_shares_bearer_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COOLAPK_HTTP_TOKEN", "tok")
    monkeypatch.setenv("COOLAPK_HTTP_HOST", "127.0.0.1")
    monkeypatch.setattr(server, "_client", None)
    monkeypatch.setattr(server, "_client_fp", None)
    app, _host, _port = server._build_http_app()
    try:
        status, body = _asgi_call(app, "/status")
        assert status == 401 and body["error"] == "unauthorized"

        status, body = _asgi_call(
            app, "/status", [(b"authorization", b"Bearer wrong")]
        )
        assert status == 401

        status, body = _asgi_call(
            app, "/status", [(b"authorization", b"Bearer tok")]
        )
        assert status == 200
        assert body["status"] == "ok"
        assert body["version"] == __version__
    finally:
        asyncio.run(server._reset_client())
        asyncio.run(server._close_retired())


# ---- B6 焦点模式不预拉默认回复 ----


class _FocusPrefetchClient:
    def __init__(self, merged, fallback=None) -> None:
        self.merged = merged
        self.fallback = fallback or []
        self.smart_kwargs: dict | None = None
        self.fallback_calls = 0

    async def get_feed_detail_smart(self, feed_id, **kwargs) -> dict:
        self.smart_kwargs = kwargs
        return {"source": "api", "data": _feed_data(), "replies": []}

    async def get_author_replies_paged(
        self, feed_id, max_pages=5, budget=None, start_page=1
    ):
        budget.take()
        return FetchResult(list(self.merged), requests=1)

    async def get_popular_replies_paged(
        self, feed_id, max_pages=5, budget=None, start_page=1
    ):
        budget.take()
        return FetchResult(list(self.merged), requests=1)

    async def get_feed_replies(self, feed_id, page=1, list_type="lastupdate_desc"):
        self.fallback_calls += 1
        return list(self.fallback)


def test_focus_skips_default_reply_prefetch(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FocusPrefetchClient([_reply(11)])
    _patch_client(monkeypatch, fake)

    payload = json.loads(
        asyncio.run(server.coolapk_feed_detail(1001, comments_focus="smart", with_replies=True))
    )

    assert fake.smart_kwargs is not None
    assert fake.smart_kwargs["with_replies"] is False, "focus 会整份替换，预拉纯属浪费"
    assert [r["id"] for r in payload["replies"]] == [11]
    assert fake.fallback_calls == 0


def test_focus_falls_back_to_default_replies(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FocusPrefetchClient([], fallback=[_reply(77)])
    _patch_client(monkeypatch, fake)

    payload = json.loads(
        asyncio.run(server.coolapk_feed_detail(1001, comments_focus="smart", with_replies=True))
    )

    assert fake.fallback_calls == 1
    assert [r["id"] for r in payload["replies"]] == [77]
    assert payload["comments_focus"]["fallback"] == "default_replies"
