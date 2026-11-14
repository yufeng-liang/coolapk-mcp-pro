"""HTTP 传输层安全链测试：/img 中转与 Bearer 鉴权。

这两块此前只有手工探针（tests/manual/regression_probe.py）覆盖，CI 里一旦
把 compare_digest 换成 ==、把跳转前校验换成跳转后校验，都不会变红。
全部离线：图片拉取走桩函数或 httpx.MockTransport，不碰真实网络。
"""

import asyncio
import hashlib
import hmac
import time

import httpx
import pytest

from coolapk_mcp import client as client_mod
from coolapk_mcp import server

SECRET = "img-secret-for-tests"
TOKEN = "test-bearer-token"


def _sign(url: str, exp: int, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), f"{url}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def _build_app(monkeypatch: pytest.MonkeyPatch, *, token: str = TOKEN, anonymous: str | None = None):
    """按当前环境变量构建真实的 Starlette 应用，MCP 端点换成计数桩。"""
    calls: list[dict] = []

    async def stub_inner(scope, receive, send):
        calls.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"inner-ok"})

    monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: stub_inner, raising=False)
    monkeypatch.setenv("COOLAPK_HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("COOLAPK_HTTP_TOKEN", token)
    monkeypatch.setenv("COOLAPK_IMG_SECRET", SECRET)
    if anonymous is None:
        monkeypatch.delenv("COOLAPK_ALLOW_ANONYMOUS", raising=False)
    else:
        monkeypatch.setenv("COOLAPK_ALLOW_ANONYMOUS", anonymous)
    app, _host, _port = server._build_http_app()
    return app, calls


async def _get(app, path: str, params: dict | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        return await c.get(path, params=params)


async def _img(app, url: str, exp: int | None = None, sig: str | None = None) -> httpx.Response:
    exp = int(time.time()) + 600 if exp is None else exp
    sig = _sign(url, exp) if sig is None else sig
    return await _get(app, "/img", {"u": url, "exp": str(exp), "sig": sig})


# ---------------- /img 签名与域名闸门 ----------------


def test_img_missing_params_is_400(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _calls = _build_app(monkeypatch)
    resp = asyncio.run(_get(app, "/img"))
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_request"


def test_img_non_integer_exp_is_400(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _calls = _build_app(monkeypatch)
    resp = asyncio.run(_get(app, "/img", {"u": "https://image.coolapk.com/a.jpg", "exp": "abc", "sig": "0" * 32}))
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_request"


def test_img_expired_link_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _calls = _build_app(monkeypatch)
    url = "https://image.coolapk.com/a.jpg"
    exp = int(time.time()) - 1
    resp = asyncio.run(_img(app, url, exp=exp))
    assert resp.status_code == 403
    assert resp.json()["error"] == "expired"


def test_img_wrong_signature_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _calls = _build_app(monkeypatch)
    url = "https://image.coolapk.com/a.jpg"
    exp = int(time.time()) + 600
    resp = asyncio.run(_img(app, url, exp=exp, sig=_sign(url, exp, secret="other-secret")))
    assert resp.status_code == 403
    assert resp.json()["error"] == "bad_signature"


def test_img_non_hex_signature_is_403_not_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """compare_digest 对含非 ASCII 的 str 会抛 TypeError，格式闸门必须先挡掉。"""
    app, _calls = _build_app(monkeypatch)
    url = "https://image.coolapk.com/a.jpg"
    exp = int(time.time()) + 600
    resp = asyncio.run(_img(app, url, exp=exp, sig="§" * 32))
    assert resp.status_code == 403
    assert resp.json()["error"] == "bad_signature"


def test_img_forbidden_host_rejected_even_with_valid_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    """签名有效也不能把服务端当内网代理；且 host 校验必须发生在建连之前。"""
    app, _calls = _build_app(monkeypatch)
    fetched: list[str] = []

    async def fake_fetch(client, target, headers):
        fetched.append(target)
        return 200, b"x", "image/jpeg", None, 0

    monkeypatch.setattr(server, "_fetch_upstream_image", fake_fetch)

    for url in (
        "http://127.0.0.1:8080/a.jpg",
        "https://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
    ):
        resp = asyncio.run(_img(app, url))
        assert resp.status_code == 403, url
        assert resp.json()["error"] == "forbidden_host", url
    assert fetched == [], "host 校验通过前不得发起任何上游请求"


def test_img_nonstandard_port_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _calls = _build_app(monkeypatch)
    resp = asyncio.run(_img(app, "https://image.coolapk.com:8443/a.jpg"))
    assert resp.status_code == 403
    assert resp.json()["error"] == "forbidden_host"


# ---------------- /img 取图与响应头 ----------------


def test_img_thumb_missing_falls_back_to_original(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _calls = _build_app(monkeypatch)
    url = "https://image.coolapk.com/a@1000w.jpg"
    seen: list[str] = []

    async def fake_fetch(client, target, headers):
        seen.append(target)
        if target.endswith(".m.jpg"):
            return 404, b"", "", None, 0
        return 200, b"jpeg-bytes", "image/jpeg", None, 0

    monkeypatch.setattr(server, "_fetch_upstream_image", fake_fetch)
    monkeypatch.delenv("COOLAPK_IMG_THUMBNAIL", raising=False)

    resp = asyncio.run(_img(app, url))

    assert resp.status_code == 200
    assert resp.content == b"jpeg-bytes"
    assert seen[0] == "https://image.coolapk.com/a@1000w.jpg.m.jpg"
    assert seen[1] == url
    assert len(seen) == 2


def test_img_sets_nosniff_and_bounded_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _calls = _build_app(monkeypatch)
    url = "https://image.coolapk.com/a.jpg"
    exp = int(time.time()) + 120

    async def fake_fetch(client, target, headers):
        return 200, b"x", "image/svg+xml", None, 0

    monkeypatch.setattr(server, "_fetch_upstream_image", fake_fetch)

    resp = asyncio.run(_img(app, url, exp=exp))

    assert resp.status_code == 200
    # svg 是活文档，必须回落成位图类型
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.headers["x-content-type-options"] == "nosniff"
    max_age = int(resp.headers["cache-control"].split("max-age=")[1])
    assert 0 <= max_age <= 120


def test_img_oversize_propagates_413(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _calls = _build_app(monkeypatch)

    async def fake_fetch(client, target, headers):
        return 0, b"", "", {"error": "image_too_large", "detail": "超过上限"}, 413

    monkeypatch.setattr(server, "_fetch_upstream_image", fake_fetch)
    resp = asyncio.run(_img(app, "https://image.coolapk.com/a.jpg"))

    assert resp.status_code == 413
    assert resp.json()["error"] == "image_too_large"


def test_img_upstream_failure_is_502(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _calls = _build_app(monkeypatch)

    async def fake_fetch(client, target, headers):
        return 500, b"", "", None, 0

    monkeypatch.setattr(server, "_fetch_upstream_image", fake_fetch)
    resp = asyncio.run(_img(app, "https://image.coolapk.com/a.jpg"))

    assert resp.status_code == 502
    assert resp.json()["error"] == "upstream_status"


# ---------------- 上游跳转与字节闸门（真实 httpx 管线） ----------------


def _mock_image_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_upstream_redirect_to_other_host_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """跳转不能被自动跟随：第二跳指向内网时必须先被 host 校验拦下。"""
    hits: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.host)
        return httpx.Response(
            302, headers={"location": "http://169.254.169.254/latest/meta-data"}
        )

    fake = _mock_image_client(handler)
    monkeypatch.setattr(client_mod, "_IMAGE_CLIENT_OVERRIDE", fake)

    status, body, ctype, err, err_status = asyncio.run(
        server._fetch_upstream_image(fake, "https://image.coolapk.com/a.jpg", {})
    )
    asyncio.run(fake.aclose())

    assert err is not None and err["error"] == "forbidden_host"
    assert err_status == 403
    assert hits == ["image.coolapk.com"], "内网地址一次都不该被真正连上"


def test_upstream_redirect_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    hits: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://image.coolapk.com/loop.jpg"})

    fake = _mock_image_client(handler)
    status, body, ctype, err, err_status = asyncio.run(
        server._fetch_upstream_image(fake, "https://image.coolapk.com/a.jpg", {})
    )
    asyncio.run(fake.aclose())

    assert err is not None and err["error"] == "too_many_redirects"
    assert err_status == 403
    assert len(hits) == 4, "最多 4 次请求（初次 + 3 跳）"


def test_upstream_declared_length_over_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": str(server.MAX_IMAGE_BYTES + 1)},
            content=b"tiny",
        )

    fake = _mock_image_client(handler)
    status, body, ctype, err, err_status = asyncio.run(
        server._fetch_upstream_image(fake, "https://image.coolapk.com/a.jpg", {})
    )
    asyncio.run(fake.aclose())

    assert err is not None and err["error"] == "image_too_large"
    assert err_status == 413


def test_read_image_body_streams_until_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "MAX_IMAGE_BYTES", 8)
    resp = httpx.Response(200, content=b"x" * 64)
    status, body, ctype, err, err_status = asyncio.run(server._read_image_body(resp))

    assert err is not None and err["error"] == "image_too_large"
    assert err_status == 413


# ---------------- Bearer 鉴权 ----------------


async def _drive(endpoint, headers: list[tuple[bytes, bytes]]) -> dict:
    out: dict = {"start": None, "body": b""}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            out["start"] = message
        elif message["type"] == "http.response.body":
            out["body"] += message.get("body", b"")

    scope = {"type": "http", "method": "POST", "path": "/mcp", "query_string": b"", "headers": headers}
    await endpoint(scope, receive, send)
    return out


def test_mcp_endpoint_rejects_missing_or_wrong_token(monkeypatch: pytest.MonkeyPatch) -> None:
    app, calls = _build_app(monkeypatch)
    nested = next(
        r.app for r in app.routes if type(r).__name__ == "Mount"
    )  # Mount("/") 上的鉴权壳

    no_auth = asyncio.run(_drive(nested, []))
    assert no_auth["start"]["status"] == 401
    assert dict(no_auth["start"]["headers"])[b"www-authenticate"] == b"Bearer"

    wrong = asyncio.run(_drive(nested, [(b"authorization", b"Bearer nope")]))
    assert wrong["start"]["status"] == 401

    basic = asyncio.run(_drive(nested, [(b"authorization", b"Basic dGVzdA==")]))
    assert basic["start"]["status"] == 401

    assert calls == [], "被拒绝的请求不该进入 MCP 应用"


def test_mcp_endpoint_accepts_correct_token(monkeypatch: pytest.MonkeyPatch) -> None:
    app, calls = _build_app(monkeypatch)
    nested = next(r.app for r in app.routes if type(r).__name__ == "Mount")

    ok = asyncio.run(_drive(nested, [(b"authorization", f"Bearer {TOKEN}".encode())]))
    assert ok["start"]["status"] == 200
    assert ok["body"] == b"inner-ok"
    assert len(calls) == 1

    # scheme 大小写不敏感（RFC 7235）
    lower = asyncio.run(_drive(nested, [(b"authorization", f"bearer {TOKEN}".encode())]))
    assert lower["start"]["status"] == 200


def test_mcp_endpoint_rejects_duplicate_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    """重复头会让反代与进程各取一个，必须整体拒绝。"""
    app, calls = _build_app(monkeypatch)
    nested = next(r.app for r in app.routes if type(r).__name__ == "Mount")

    dup = asyncio.run(
        _drive(
            nested,
            [
                (b"authorization", f"Bearer {TOKEN}".encode()),
                (b"authorization", b"Bearer nope"),
            ],
        )
    )
    assert dup["start"]["status"] == 401
    assert calls == []


def test_http_app_refuses_to_start_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """回环地址也不再免鉴权：本机浏览器 + DNS rebinding 一样能打到 127.0.0.1。"""
    monkeypatch.setattr(
        server.mcp, "streamable_http_app", lambda: (lambda s, r, sn: None), raising=False
    )
    monkeypatch.setenv("COOLAPK_HTTP_HOST", "127.0.0.1")
    monkeypatch.delenv("COOLAPK_HTTP_TOKEN", raising=False)
    monkeypatch.delenv("COOLAPK_ALLOW_ANONYMOUS", raising=False)

    with pytest.raises(SystemExit) as exc:
        server._build_http_app()
    assert "COOLAPK_ALLOW_ANONYMOUS" in str(exc.value)

    monkeypatch.setenv("COOLAPK_ALLOW_ANONYMOUS", "1")
    app, _host, _port = server._build_http_app()
    assert app is not None, "显式开匿名开关时允许构建"
