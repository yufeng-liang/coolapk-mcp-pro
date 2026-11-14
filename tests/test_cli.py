"""CLI 层测试：参数透传、错误输出口径、ADB 失败诊断

CLI 不走 MCP，历史上一条测试都没有——参数漂移（server 有新开关而 cli 没接）
就是这样长期无人发现的，这里锁住几条最容易退化的契约。
"""
from __future__ import annotations

import json
import subprocess

import pytest
from click.testing import CliRunner

from coolapk_mcp import cli as cli_mod
from coolapk_mcp import focus as focus_mod
from coolapk_mcp.client import CoolapkError


class _FakeClient:
    """最小假客户端：记录调用，异步上下文管理器语义与真实 client 一致"""

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.search_result: object = {}
        self.fallback_replies: list = []
        self.detail_result: dict = {
            "data": {"id": 1, "replynum": 0},"replies": None,"source": "api",
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _record(self, name, args, kwargs):
        self.calls.append((name, args, kwargs))

    async def search(self, keyword, search_type="feed", page=1, sort="hot"):
        self._record("search", (keyword, search_type, page), {"sort": sort})
        return self.search_result

    async def get_feed_detail_smart(self, feed_id, **kwargs):
        self._record("get_feed_detail_smart", (feed_id,), kwargs)
        return self.detail_result

    async def get_feed_history(self, feed_id):
        self._record("get_feed_history", (feed_id,), {})
        return []

    async def get_feed_replies(self, feed_id, page=1):
        self._record("get_feed_replies", (feed_id, page), {})
        return self.fallback_replies


@pytest.fixture()
def fake_client(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(cli_mod, "CoolapkClient", lambda *a, **k: fake)
    monkeypatch.setattr(focus_mod, "collect_focus", _no_focus)
    return fake


async def _no_focus(*args, **kwargs):
    return [], None


def _invoke(args):
    return CliRunner().invoke(cli_mod.cli, args)


def test_help_lists_commands():
    """入口点装配完好：--help 能跑出命令清单"""
    result = _invoke(["--help"])
    assert result.exit_code == 0
    assert "search" in result.output and "login" in result.output


def test_search_forwards_sort(fake_client):
    """--sort 必须真的传进 client.search（旧版 cli 不传，与 server 漂移）"""
    result = _invoke(["search", "测试词", "--sort", "reply"])
    assert result.exit_code == 0, result.output
    name, args, kwargs = fake_client.calls[0]
    assert name == "search"
    assert args[0] == "测试词"
    assert kwargs["sort"] == "reply"


def test_feed_forwards_focus_and_flags(fake_client):
    """--focus smart 触发焦点收敛；focus 活跃时不预拉默认回复（与服务端同源）"""
    result = _invoke(["feed", "1", "--focus", "smart", "--history", "--expand-sub-replies"])
    assert result.exit_code == 0, result.output
    kwargs = next(kw for n, _a, kw in fake_client.calls if n == "get_feed_detail_smart")
    assert kwargs["with_replies"] is False, "focus 会整份替换 replies，预拉的那页没人看"
    # --history 由 cli 自己抓历史（get_feed_history），不该塞进 smart 的入参
    assert "with_history" not in kwargs


def test_feed_focus_falls_back_to_default_replies(fake_client):
    """聚焦两路都空时回拉默认回复：有评论的帖子不该显示成空评论区"""
    fake_client.fallback_replies = [{"id": 7, "message": "普通评论"}]

    result = _invoke(["feed", "1", "--focus", "smart"])

    assert result.exit_code == 0, result.output
    assert any(name == "get_feed_replies" for name, _a, _kw in fake_client.calls)
    payload = json.loads(result.output)
    assert [r["id"] for r in payload["replies"]] == [7]
    assert payload["comments_focus"]["fallback"] == "default_replies"


def test_feed_focus_page_is_forwarded(fake_client, monkeypatch):
    """--focus-page 对应服务端的 focus_page：续抓时从上次的 next_page 开始"""
    seen: dict = {}

    async def _focus(client, feed_id, mode, start_page=1):
        seen["start_page"] = start_page
        return [], None

    monkeypatch.setattr(focus_mod, "collect_focus", _focus)

    result = _invoke(["feed", "1", "--focus", "hot", "--focus-page", "3"])

    assert result.exit_code == 0, result.output
    assert seen["start_page"] == 3


def test_feed_grep_searches_before_reply_cap(fake_client):
    """--grep 命中落在第 70 条也必须找到：检索先于条数截断（服务端同源）"""
    fake_client.detail_result = {
        "data": {"id": 1, "replynum": 100},
        "replies": [
            {
                "id": index,
                "message": "这里有解锁工具" if index == 70 else f"第{index}条",
                "userInfo": {"username": "网友"},
            }
            for index in range(100)
        ],
        "source": "api",
    }

    result = _invoke(["feed", "1", "--grep", "解锁工具"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["comment_search"]["scanned_comments"] == 100
    assert [r["id"] for r in payload["replies"]] == [70]


def test_output_error_carries_retry_hints(capsys):
    """CLI 错误输出与服务端同口径：retry_after / retryable"""
    err = CoolapkError("限流了", kind="rate_limit", retryable=True, retry_after=12.34)
    with pytest.raises(SystemExit):
        cli_mod._output_error(err)
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_type"] == "rate_limit"
    assert payload["retryable"] is True
    assert payload["retry_after"] == 12.3


def test_output_error_omits_hints_when_absent(capsys):
    """没有提示就别输出空字段（下游按字段存在性判断）"""
    err = CoolapkError("业务错误", kind="api")
    with pytest.raises(SystemExit):
        cli_mod._output_error(err)
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"error", "code", "error_type"}


def test_adb_failure_prints_diagnosis(monkeypatch, capsys):
    """adb 不可用时必须给出原因，不能静默返回 None"""

    def boom(*args, **kwargs):
        raise FileNotFoundError("adb")

    # subprocess 是函数内 import 的，只能打全局模块对象
    monkeypatch.setattr(subprocess, "run", boom)
    assert cli_mod._extract_cookie_from_adb() is None
    err = capsys.readouterr().err
    assert "ADB 提取失败" in err
    assert "FileNotFoundError" in err
