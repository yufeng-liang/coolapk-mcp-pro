"""D 组复盘补测：CLI 失败契约、密钥同源、图片外链边界、指标与 /status 降级"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

import httpx
import pytest
from click.testing import CliRunner

from coolapk_mcp import cli as cli_mod
from coolapk_mcp import img_link
from coolapk_mcp import logging_utils
from coolapk_mcp import server as server_mod
from coolapk_mcp import throttle
from coolapk_mcp.client import CoolapkClient, CoolapkError
from coolapk_mcp.config import AppConfig

BASE = "https://mcp.example.com"


class _FakeClient:
    """可注入异常 / 空数据的假客户端"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.user_space: list = [{"uid": 1, "username": "tester", "level": 1}]
        self.suggest: list = [{"title": "Magisk 模块"}]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_app_detail(self, package_name: str):
        self.calls.append(("app_detail", package_name))
        return [{"id": 1, "title": "酷安", "pkgname": package_name}]

    async def get_user_space(self, uid: int):
        self.calls.append(("user_space", uid))
        return self.user_space

    async def get_topic_suggest(self, keyword: str):
        self.calls.append(("suggest", keyword))
        return self.suggest


@pytest.fixture()
def fake_client(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    fake = _FakeClient()
    monkeypatch.setattr(cli_mod, "CoolapkClient", lambda *a, **k: fake)
    return fake


def _run(args: list[str]) -> tuple[int, str]:
    result = CliRunner().invoke(cli_mod.cli, args)
    return result.exit_code, result.output


# ---- CLI 失败契约：脚本与 CI 靠退出码 + error JSON 判断成败 ----


def test_cli_failure_exits_one_with_error_json(fake_client: _FakeClient) -> None:
    async def boom(package_name: str):
        raise CoolapkError("请求被风控拦截", error_code=403, kind="risk")

    fake_client.get_app_detail = boom  # type: ignore[method-assign]
    code, out = _run(["app-detail", "com.coolapk.market"])

    assert code == 1
    assert json.loads(out) == {"error": "请求被风控拦截", "code": 403, "error_type": "risk"}


def test_cli_retryable_error_carries_hints(fake_client: _FakeClient) -> None:
    async def boom(package_name: str):
        raise CoolapkError(
            "限流中", error_code=429, kind="transient", retry_after=12.34, retryable=True
        )

    fake_client.get_app_detail = boom  # type: ignore[method-assign]
    code, out = _run(["app-detail", "com.coolapk.market"])

    assert code == 1
    payload = json.loads(out)
    assert payload["retry_after"] == 12.3
    assert payload["retryable"] is True


def test_cli_author_without_profile_exits_one(fake_client: _FakeClient) -> None:
    fake_client.user_space = []

    code, out = _run(["author", "1"])

    assert code == 1
    assert json.loads(out) == {"error": "无数据"}


def test_cli_suggest_skips_non_dict_cards(fake_client: _FakeClient) -> None:
    fake_client.suggest = [None, "junk", {"title": "搜索用户：酷安"}, {"title": "Magisk 模块"}]

    code, out = _run(["suggest", "面具"])

    assert code == 0
    assert json.loads(out) == {"suggests": ["Magisk 模块"]}


# ---- CLI 产出链接 × 端点签名同源：不 patch 密钥，真走 COOLAPK_IMG_SECRET ----


def test_cli_image_link_uses_resolved_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COOLAPK_IMG_SECRET", "real-secret-value")
    monkeypatch.setenv("COOLAPK_PUBLIC_BASE", BASE)
    raw = "https://image.coolapk.com/a.jpg?x=1&y=%2F"

    code, out = _run(["image-link", raw, "--ttl", "7"])

    assert code == 0
    item = json.loads(out)["items"][0]
    parsed = httpx.URL(item["link"])
    assert parsed.params["u"] == raw
    assert parsed.params["sig"] == img_link.sign_image(
        raw, "real-secret-value", int(parsed.params["exp"])
    )
    assert item["expires_at"] == int(parsed.params["exp"])


# ---- img_link 输入边界 ----


def test_img_link_dedupes_repeated_urls() -> None:
    payload = img_link.build_image_links(
        "https://image.coolapk.com/a.jpg https://image.coolapk.com/a.jpg", BASE, "s"
    )

    assert payload["count"] == 1


def test_img_link_keeps_comma_inside_url() -> None:
    raw = "https://image.coolapk.com/a.jpg?ids=1,2"

    payload = img_link.build_image_links(raw, BASE, "s")

    assert payload["count"] == 1
    assert payload["items"][0]["url"] == raw
    assert "error" not in payload["items"][0]


def test_img_link_still_accepts_comma_separated_urls() -> None:
    payload = img_link.build_image_links(
        "https://image.coolapk.com/a.jpg, https://avatar.coolapk.com/b.png", BASE, "s"
    )

    assert [i["url"] for i in payload["items"]] == [
        "https://image.coolapk.com/a.jpg",
        "https://avatar.coolapk.com/b.png",
    ]


def test_img_link_rejects_empty_public_base() -> None:
    payload = img_link.build_image_links("https://image.coolapk.com/a.jpg", "  ", "s")

    assert payload["count"] == 0
    assert "public_base" in payload["error"]


def test_img_link_strips_base_trailing_slash() -> None:
    payload = img_link.build_image_links("https://image.coolapk.com/a.jpg", BASE + "/", "s")

    link = payload["items"][0]["link"]
    assert link.startswith(BASE + "/img?")
    assert "//img" not in link.replace("https://", "")


def test_clamp_ttl_handles_inf_and_bool() -> None:
    assert img_link.clamp_ttl(float("inf")) == img_link.DEFAULT_TTL_MINUTES
    assert img_link.clamp_ttl(True) == img_link.DEFAULT_TTL_MINUTES
    assert img_link.clamp_ttl(10081) == img_link.MAX_TTL_MINUTES
    assert img_link.clamp_ttl(-5) == 1


# ---- /status 配置损坏时的降级 ----


class _StatusStub:
    def cache_stats(self) -> dict:
        return {"entries": 0, "bytes": 0}

    def metrics(self) -> dict:
        return {"requests": 0}


def test_status_payload_degrades_when_config_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(cls):
        raise OSError("config.json 损坏")

    monkeypatch.setattr(server_mod.AppConfig, "load", classmethod(boom))

    payload = asyncio.run(server_mod._status_payload(_StatusStub()))  # type: ignore[arg-type]

    assert payload["status"] == "ok"
    assert payload["login"] == {"logged_in": False, "uid": None, "mode": "unknown"}


# ---- 结构化日志：异常行与非法级别 ----


def test_json_formatter_keeps_last_exception_line() -> None:
    try:
        raise ValueError("boom-中文")
    except ValueError:
        record = logging.LogRecord(
            "coolapk_mcp.test", logging.ERROR, __file__, 1, "失败：细节", (), sys.exc_info()
        )

    line = logging_utils.JsonFormatter().format(record)

    assert chr(10) not in line
    assert json.loads(line)["exc"] == "ValueError: boom-中文"


def test_setup_logging_falls_back_on_invalid_level(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COOLAPK_LOG_JSON", "1")
    monkeypatch.setenv("COOLAPK_LOG_LEVEL", "verbose")
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        logging_utils.setup_logging()

        assert root.level == logging.INFO
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
    assert "不是有效日志级别" in capsys.readouterr().err


# ---- 指标：重试、错误归类与延迟 ----


_STALE_CLIENTS: list = []  # 异步用例里排队关闭的旧 client，留引用防 GC


def _client_with(handler) -> CoolapkClient:
    cfg = AppConfig()
    cfg.uid = "123"
    cfg.token = "tok"
    client = CoolapkClient(cfg)
    stale = client._client
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    # __init__ 建出来的 client 已没人引用：立刻关掉，别把连接池句柄留到进程结束。
    # 异步用例本身已在事件循环里（asyncio.run 会拒绝嵌套），改成排个任务并留引用
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(stale.aclose())
    else:
        _STALE_CLIENTS.append(running.create_task(stale.aclose()))
    return client


def test_metrics_tracks_retries_errors_and_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(200, json={"data": {"ok": 1}})
        return httpx.Response(500, json={"message": "boom"})

    async def no_sleep(attempt: int, floor: float = 0.0) -> None:
        return None

    monkeypatch.setattr(throttle, "backoff_sleep", no_sleep)
    client = _client_with(handler)

    async def main() -> dict:
        try:
            await client.get("/v6/feed/detail?id=1")
            with pytest.raises(CoolapkError):
                await client.get("/v6/feed/detail?id=2")
            return client.metrics()
        finally:
            await client.close()

    metrics = asyncio.run(main())

    assert metrics["calls"] == 2
    assert metrics["requests"] >= 2  # 第二次含重试
    assert metrics["errors"]["transient"] == 1
    assert metrics["retries"] >= 1
    assert metrics["latency_ms"]["max"] >= metrics["latency_ms"]["avg"] > 0


def test_metrics_records_unexpected_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise ValueError("unexpected crash")

    client = _client_with(handler)

    async def main() -> dict:
        try:
            with pytest.raises(ValueError):
                await client.get("/v6/feed/detail?id=3")
            return client.metrics()
        finally:
            await client.close()

    metrics = asyncio.run(main())

    # 非 CoolapkError 也要留痕，否则 /status 会显示零错误而调用一直在炸
    assert metrics["errors"] == {"internal:ValueError": 1}
    assert metrics["calls"] == 1


def test_shared_ride_counts_single_cache_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(throttle, "CACHE_ENABLED", True)
    gate: asyncio.Event | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        assert gate is not None
        await gate.wait()
        return httpx.Response(200, json={"data": {"ok": 1}})

    client = _client_with(handler)

    async def main() -> dict:
        nonlocal gate
        gate = asyncio.Event()
        first = asyncio.create_task(client.get("/v6/feed/detail?id=9"))
        for _ in range(200):
            if client.metrics()["requests"] >= 1:
                break
            await asyncio.sleep(0)
        second = asyncio.create_task(client.get("/v6/feed/detail?id=9"))
        for _ in range(200):
            if client.metrics()["shared"] >= 1:
                break
            await asyncio.sleep(0)
        gate.set()
        try:
            await asyncio.gather(first, second)
            return client.metrics()
        finally:
            await client.close()

    metrics = asyncio.run(main())

    assert metrics["requests"] == 1  # 上游只被打一次
    assert metrics["shared"] == 1
    # 搭车者不该被算成「缓存未命中」
    assert metrics["cache_misses"] == 1
