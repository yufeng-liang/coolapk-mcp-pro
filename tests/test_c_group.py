"""C 组（安全余项）：凭据权限、token 自检、DNS rebinding 白名单、/img HEAD。"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
from urllib.parse import quote

import httpx
import pytest

from coolapk_mcp import config, server


def _asgi_raw(app, path: str, method: str = "GET", headers=None):
    """打一次 ASGI 请求，返回 (status, headers dict, body bytes)。"""
    path_only, _, query = path.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path_only,
        "raw_path": path_only.encode(),
        "query_string": query.encode(),
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
    resp_headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    return start["status"], resp_headers, body


def test_restrict_to_owner_sets_posix_mode(tmp_path: pytest.TempPathFactory) -> None:
    if os.name != "posix":
        pytest.skip("POSIX 权限位")
    target = tmp_path / "secret.txt"
    target.write_text("t", encoding="utf-8")
    os.chmod(target, 0o644)
    config.restrict_to_owner(target)
    assert target.stat().st_mode & 0o777 == 0o600


def test_config_save_restricts_credentials(monkeypatch, tmp_path) -> None:
    calls = []
    monkeypatch.setattr(config, "restrict_to_owner", lambda p, mode=0o600: calls.append(p))
    cfg = config.AppConfig()
    cfg.uid = "123"
    cfg.token = "tok"
    cfg.save()
    assert calls == [config.CONFIG_FILE]
    assert json.loads(config.CONFIG_FILE.read_text(encoding="utf-8"))["token"] == "tok"


def test_warn_token_weakness_flags_short_and_padded(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="coolapk_mcp.server")
    server._warn_token_weakness(" short ")
    text = caplog.text
    assert "首尾空白" in text
    assert "仅 7 位" in text


def test_warn_token_weakness_flags_example_value(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="coolapk_mcp.server")
    server._warn_token_weakness("coolapk-mcp-dev")
    assert "示例值" in caplog.text


def test_warn_token_weakness_accepts_strong_token(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="coolapk_mcp.server")
    server._warn_token_weakness("x" * 32)
    assert "COOLAPK_HTTP_TOKEN" not in caplog.text


def test_transport_security_defaults_to_disabled(monkeypatch) -> None:
    monkeypatch.delenv("COOLAPK_ALLOWED_HOSTS", raising=False)
    settings = server._transport_security_settings()
    assert settings.enable_dns_rebinding_protection is False


def test_transport_security_enables_with_host_whitelist(monkeypatch) -> None:
    monkeypatch.setenv("COOLAPK_ALLOWED_HOSTS", "127.0.0.1:8000, mcp.example.com")
    monkeypatch.delenv("COOLAPK_ALLOWED_ORIGINS", raising=False)
    settings = server._transport_security_settings()
    assert settings.enable_dns_rebinding_protection is True
    assert settings.allowed_hosts == ["127.0.0.1:8000", "mcp.example.com"]
    assert settings.allowed_origins == ["*"]

    monkeypatch.setenv("COOLAPK_ALLOWED_ORIGINS", "https://a.example")
    settings = server._transport_security_settings()
    assert settings.allowed_origins == ["https://a.example"]


def _upstream(seen: list[str], body: bytes = b"IMG") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        if request.method == "HEAD":
            return httpx.Response(
                200,
                headers={"content-type": "image/png", "content-length": "1048576"},
            )
        return httpx.Response(200, headers={"content-type": "image/png"}, content=body)

    return httpx.MockTransport(handler)


def test_fetch_upstream_image_head_skips_body() -> None:
    seen: list[str] = []
    client = httpx.AsyncClient(transport=_upstream(seen))
    status, body, ctype, err, _ = asyncio.run(
        server._fetch_upstream_image(
            client, "https://image.coolapk.com/a.jpg", {}, method="HEAD"
        )
    )
    assert status == 200 and body == b"" and ctype == "image/png" and err is None
    assert seen == ["HEAD"]


def test_fetch_upstream_image_get_reads_body() -> None:
    seen: list[str] = []
    client = httpx.AsyncClient(transport=_upstream(seen))
    status, body, _ctype, err, _ = asyncio.run(
        server._fetch_upstream_image(client, "https://image.coolapk.com/a.jpg", {})
    )
    assert status == 200 and body == b"IMG" and err is None
    assert seen == ["GET"]


def _img_app(monkeypatch, url: str, exp: int) -> tuple[object, str]:
    monkeypatch.setenv("COOLAPK_ALLOW_ANONYMOUS", "1")
    monkeypatch.setenv("COOLAPK_IMG_SECRET", "c-secret")
    monkeypatch.delenv("COOLAPK_HTTP_TOKEN", raising=False)
    seen: list[str] = []
    monkeypatch.setattr(server, "image_client", lambda direct=False: httpx.AsyncClient(transport=_upstream(seen)))
    app, _host, _port = server._build_http_app()
    sig = hmac.new(
        b"c-secret", f"{url}|{exp}".encode(), hashlib.sha256
    ).hexdigest()[:32]
    query = f"u={quote(url, safe='')}&exp={exp}&sig={sig}"
    return app, query, seen


def test_img_head_returns_no_body(monkeypatch) -> None:
    url = "https://image.coolapk.com/a.jpg"
    app, query, seen = _img_app(monkeypatch, url, int(__import__("time").time()) + 600)
    status, headers, body = _asgi_raw(app, f"/img?{query}", method="HEAD")
    assert status == 200 and body == b""
    assert headers["content-type"] == "image/png"
    assert headers["x-content-type-options"] == "nosniff"
    assert seen == ["HEAD"]


def test_img_get_returns_bytes(monkeypatch) -> None:
    url = "https://image.coolapk.com/a.jpg"
    app, query, seen = _img_app(monkeypatch, url, int(__import__("time").time()) + 600)
    status, _headers, body = _asgi_raw(app, f"/img?{query}")
    assert status == 200 and body == b"IMG"
    assert seen == ["GET"]
