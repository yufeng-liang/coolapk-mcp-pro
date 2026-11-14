"""D 组收尾：图片外链单一实现、结构化日志、延迟指标与 CLI 新命令"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging

import httpx
import pytest
from click.testing import CliRunner

from coolapk_mcp import cli as cli_mod
from coolapk_mcp import img_link
from coolapk_mcp import logging_utils
from coolapk_mcp import server as server_mod
from coolapk_mcp.budget import FetchResult
from coolapk_mcp.client import CoolapkClient
from coolapk_mcp.config import AppConfig


# ---- D1 CLI 新命令 ----


class _FakeClient:
    """最小假客户端：覆盖 app-detail / author / suggest / get-url 四个新命令"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_app_detail(self, package_name: str):
        self.calls.append(("app_detail", package_name))
        return [{"id": 1, "title": "酷安", "pkgname": package_name, "followNum": 10}]

    async def get_user_space(self, uid: int):
        self.calls.append(("user_space", uid))
        return [
            {
                "uid": uid,
                "username": "tester",
                "bio": "刷机与 Magisk 模块",
                "level": 10,
                "fans": 40000,
                "feed": 100,
            }
        ]

    async def get_user_feeds(self, uid: int, page: int = 1):
        self.calls.append(("user_feeds", uid, page))
        return [
            {
                "id": 11,
                "entityType": "feed",
                "message": "Magisk 模块推荐",
                "likenum": 100,
                "replynum": 10,
                "forwardnum": 1,
            },
            {"id": 12, "entityType": "feed", "message": "日常", "likenum": 5, "replynum": 1},
        ]

    async def get_user_feeds_pages(self, uid: int, pages: int = 1, budget=None):
        """author 命令走多页接口，带分页元数据"""
        self.calls.append(("user_feeds_pages", uid, pages))
        return FetchResult(
            [
                {
                    "id": 11,
                    "entityType": "feed",
                    "message": "Magisk 模块推荐",
                    "likenum": 100,
                    "replynum": 10,
                    "forwardnum": 1,
                },
                {"id": 12, "entityType": "feed", "message": "日常", "likenum": 5, "replynum": 1},
            ],
            truncated=True,
            next_page=3,
            requests=2,
        )

    async def get_notifications(self, ntype: str, page: int = 1):
        self.calls.append(("notifications", ntype, page))
        return [{"id": 1, "dateline": 1700000000}]

    async def get_notification_count(self):
        self.calls.append(("notification_count",))
        return {"message": 2}

    async def download_image(self, url: str):
        self.calls.append(("download_image", url))
        return b"IMG-BYTES", "png"

    async def get_topic_suggest(self, keyword: str):
        self.calls.append(("suggest", keyword))
        return [{"title": "搜索用户：酷安"}, {"title": "Magisk 模块"}, {"title": ""}]

    async def get_url(self, path: str):
        self.calls.append(("get_url", path))
        return {"data": {"token": "secret-token", "value": 1}}


@pytest.fixture()
def fake_client(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    fake = _FakeClient()
    monkeypatch.setattr(cli_mod, "CoolapkClient", lambda *a, **k: fake)
    return fake


def _run(args: list[str]) -> tuple[int, str]:
    result = CliRunner().invoke(cli_mod.cli, args)
    return result.exit_code, result.output


def test_cli_app_detail_outputs_model(fake_client: _FakeClient) -> None:
    code, out = _run(["app-detail", "com.coolapk.market"])

    assert code == 0
    payload = json.loads(out)
    assert payload["package_name"] == "com.coolapk.market"
    assert payload["follow_num"] == 10
    assert fake_client.calls[0] == ("app_detail", "com.coolapk.market")


def test_cli_author_scores_and_infers_expertise(fake_client: _FakeClient) -> None:
    code, out = _run(["author", "123", "--top", "5"])

    assert code == 0
    payload = json.loads(out)
    assert payload["author_tier"]  # 40000 粉丝 / 10 级 → 至少 expert
    assert payload["expertise"]  # bio 命中「刷机/模块」
    assert payload["top_posts"][0]["id"] == 11  # 100 赞胜过 5 赞
    assert payload["top_posts"][0]["post_score"] > payload["top_posts"][1]["post_score"]
    # 与服务端同口径：走多页接口 + discussion_ratio + 分页元数据
    assert fake_client.calls[1] == ("user_feeds_pages", 123, 2)
    assert payload["top_posts"][0]["discussion_ratio"] == 0.1
    assert (payload["pages"], payload["fetched"]) == (2, 2)
    assert payload["truncated"] is True
    assert payload["next_page"] == 3


def test_cli_author_pages_option_reaches_client(fake_client: _FakeClient) -> None:
    code, out = _run(["author", "123", "--pages", "3", "--top", "1"])

    assert code == 0
    assert fake_client.calls[1] == ("user_feeds_pages", 123, 3)
    assert json.loads(out)["pages"] == 3


def test_cli_author_keeps_posts_texts_when_top_zero(fake_client: _FakeClient) -> None:
    """--top 0 只该砍 top_posts，专长推断用的正文不能被一起砍掉"""
    code, out = _run(["author", "123", "--top", "0"])

    assert code == 0
    payload = json.loads(out)
    assert payload["top_posts"] == []
    assert payload["posts_texts"] == ["Magisk 模块推荐", "日常"]
    assert payload["expertise"]


def test_cli_app_detail_rejects_blank_package(fake_client: _FakeClient) -> None:
    """空包名不该发请求，要和服务端一样给出可读错误"""
    code, out = _run(["app-detail", "   "])

    assert code == 1
    payload = json.loads(out)
    assert payload["error"] == "package_name 不能为空"
    assert payload["error_type"] == "api"
    assert fake_client.calls == []


def test_cli_notify_with_count(fake_client: _FakeClient) -> None:
    code, out = _run(["notify", "message", "--with-count"])

    assert code == 0
    payload = json.loads(out)
    assert payload["items"][0]["id"] == 1
    assert payload["counts"] == {"message": 2}
    assert ("notification_count",) in fake_client.calls


def test_cli_notify_default_shape_unchanged(fake_client: _FakeClient) -> None:
    """不加 --with-count 时保持旧输出（裸列表），避免破坏既有调用方"""
    code, out = _run(["notify", "message"])

    assert code == 0
    assert json.loads(out) == [{"id": 1, "dateline": 1700000000}]
    assert ("notification_count",) not in fake_client.calls


def test_cli_image_saves_bytes(fake_client: _FakeClient, tmp_path) -> None:
    target = tmp_path / "out" / "pic.png"
    code, out = _run(["image", "https://image.coolapk.com/a/b.png", "-o", str(target)])

    assert code == 0
    assert target.read_bytes() == b"IMG-BYTES"
    payload = json.loads(out)
    assert payload["bytes"] == 9
    assert payload["format"] == "png"


def test_cli_login_status_reports_device_code() -> None:
    code, out = _run(["login", "--status"])

    assert code == 0
    payload = json.loads(out)
    assert payload["device_code"] in (True, False)
    assert "sessid_ok" in payload


def test_cli_suggest_drops_prefix_cards(fake_client: _FakeClient) -> None:
    code, out = _run(["suggest", "面具"])

    assert code == 0
    assert json.loads(out) == {"suggests": ["Magisk 模块"]}


def test_cli_get_url_strips_sensitive_keys(fake_client: _FakeClient) -> None:
    code, out = _run(["get-url", "/v6/account/checkLoginInfo"])

    assert code == 0
    assert "secret-token" not in out
    assert json.loads(out) == {"data": {"value": 1}}


def test_cli_image_link_requires_public_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COOLAPK_PUBLIC_BASE", raising=False)

    code, out = _run(["image-link", "https://image.coolapk.com/a.jpg"])

    assert code == 1
    assert "COOLAPK_PUBLIC_BASE" in json.loads(out)["error"]


def test_cli_image_link_builds_signed_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COOLAPK_PUBLIC_BASE", "https://mcp.example.com/")
    monkeypatch.setattr(server_mod, "_resolve_img_secret", lambda token: "s3cret")

    code, out = _run(
        [
            "image-link",
            "https://image.coolapk.com/a.jpg",
            "https://evil.example.com/b.jpg",
            "--ttl",
            "10",
        ]
    )

    assert code == 0
    payload = json.loads(out)
    assert payload["count"] == 2
    assert payload["ttl_minutes"] == 10
    good, bad = payload["items"]
    assert good["link"].startswith("https://mcp.example.com/img?u=")
    assert len(good["link"].split("sig=")[-1]) == 32
    assert "error" in bad


# ---- 图片外链单一实现 ----


def test_img_link_signature_is_stable() -> None:
    payload = img_link.build_image_links(
        "https://image.coolapk.com/a.jpg",
        "https://mcp.example.com",
        "s3cret",
        ttl_minutes=1,
        now=1000.0,
    )

    item = payload["items"][0]
    assert item["expires_at"] == 1060
    expected = hmac.new(
        b"s3cret", b"https://image.coolapk.com/a.jpg|1060", hashlib.sha256
    ).hexdigest()[:32]
    assert item["link"].endswith(f"exp=1060&sig={expected}")


def test_img_link_rejects_foreign_host() -> None:
    payload = img_link.build_image_links(
        "https://evil.example.com/a.jpg", "https://mcp.example.com", "s3cret"
    )

    assert payload["count"] == 1
    assert "只支持酷安图片域名" in payload["items"][0]["error"]


def test_clamp_ttl_bounds() -> None:
    assert img_link.clamp_ttl(0) == 1
    assert img_link.clamp_ttl(99999) == img_link.MAX_TTL_MINUTES
    assert img_link.clamp_ttl("abc") == img_link.DEFAULT_TTL_MINUTES


# ---- 结构化日志 ----


def test_json_formatter_emits_single_line() -> None:
    record = logging.LogRecord(
        "coolapk_mcp.test", logging.INFO, __file__, 1, "hello %s", ("world",), None
    )

    line = logging_utils.JsonFormatter().format(record)

    assert chr(10) not in line
    payload = json.loads(line)
    assert payload["level"] == "INFO"
    assert payload["msg"] == "hello world"
    assert payload["logger"] == "coolapk_mcp.test"
    assert payload["ts"].endswith("+00:00")


def test_setup_logging_is_noop_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COOLAPK_LOG_JSON", raising=False)
    root = logging.getLogger()
    before = list(root.handlers)

    logging_utils.setup_logging()

    assert root.handlers == before


def test_setup_logging_installs_json_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COOLAPK_LOG_JSON", "1")
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    foreign = logging.NullHandler()  # 模拟宿主（pytest/uvicorn）已装的 handler
    try:
        root.handlers[:] = [foreign]
        logging_utils.setup_logging("debug")

        assert root.level == logging.DEBUG
        # 只替换自己上次装的那个：宿主 handler 必须留着
        assert foreign in root.handlers
        ours = [h for h in root.handlers if isinstance(h.formatter, logging_utils.JsonFormatter)]
        assert len(ours) == 1

        # 幂等：再调一次不会叠出第二个 JSON handler
        logging_utils.setup_logging("debug")
        again = [h for h in root.handlers if isinstance(h.formatter, logging_utils.JsonFormatter)]
        assert len(again) == 1
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


# ---- 延迟指标 ----


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


async def test_metrics_records_latency() -> None:
    client = _client_with(lambda request: httpx.Response(200, json={"data": {"ok": 1}}))

    await client.get("/v6/feed/detail?id=1")
    metrics = client.metrics()

    assert metrics["calls"] == 1
    assert metrics["requests"] == 1
    assert metrics["latency_ms"]["total"] >= 0
    assert metrics["latency_ms"]["avg"] == metrics["latency_ms"]["total"]
    await client.close()
