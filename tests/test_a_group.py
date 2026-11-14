"""A 组收口测试：版本口径、页大小、页级超时、get_url 严格模式、配置解析缓存、关键词归一化。"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

import coolapk_mcp
from coolapk_mcp import client as client_mod
from coolapk_mcp import config as config_mod
from coolapk_mcp import server


def test_package_version_matches_pyproject() -> None:
    """包内 __version__ 与 pyproject 的 version 必须一致，避免再漂移"""
    root = Path(__file__).resolve().parents[1]
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    assert match is not None
    assert match.group(1) == coolapk_mcp.__version__


def _bare_client() -> client_mod.CoolapkClient:
    """只测分页编排，不建连接池"""
    return object.__new__(client_mod.CoolapkClient)


async def test_collect_paged_uses_page_size(monkeypatch) -> None:
    c = _bare_client()
    c.PAGE_SIZE = 3
    pages = {
        1: [{"id": 1}, {"id": 2}, {"id": 3}],
        2: [{"id": 4}, {"id": 5}, {"id": 6}],
        3: [{"id": 7}],
    }

    async def fetch(page: int):
        return pages[page]

    result = await c._collect_paged(fetch, max_pages=5)
    assert len(result) == 7
    assert result.truncated is False
    assert result.requests == 3


async def test_collect_paged_page_timeout_keeps_partial() -> None:
    """单页超时按截断处理，已取到的页不丢"""
    c = _bare_client()
    c.PAGE_SIZE = 2

    async def fetch(page: int):
        if page == 1:
            return [{"id": 1}, {"id": 2}]
        raise asyncio.TimeoutError

    result = await c._collect_paged(fetch, max_pages=3)
    assert len(result) == 2
    assert result.truncated is True
    assert result.next_page == 2


def test_get_url_strict_is_default_on() -> None:
    """严格模式默认开启：未知形态的路径直接拒绝"""
    assert client_mod.CoolapkClient.GET_URL_STRICT is True
    with pytest.raises(client_mod.CoolapkError) as exc:
        client_mod.CoolapkClient._check_get_url_path("/v6/feed/doit")
    assert "严格模式" in str(exc.value)


def test_get_url_strict_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setattr(client_mod.CoolapkClient, "GET_URL_STRICT", False)
    assert client_mod.CoolapkClient._check_get_url_path("/v6/feed/doit")


def test_read_json_cached_reuses_and_invalidates(tmp_path) -> None:
    """配置文件按 stat 签名缓存：未变不重复解析，变了立刻失效"""
    path = tmp_path / "cached.json"
    path.write_text('{"a": 1}', encoding="utf-8")
    first = config_mod._read_json_cached(path)
    assert config_mod._read_json_cached(path) is first
    path.write_text('{"a": 22}', encoding="utf-8")
    assert config_mod._read_json_cached(path)["a"] == 22


def test_prepare_feeds_normalizes_topic_marks() -> None:
    """keyword 与正文都走同一归一化，#解锁# 能命中「解锁」"""
    raw = [
        {"id": 1, "message": "#解锁# BL 教程"},
        {"id": 2, "message": "刷机日常"},
    ]
    items, _ = server._prepare_feeds(raw, "#解锁#", "hot")
    assert [it["id"] for it in items] == [1]

def test_get_url_strict_allows_known_read_shapes() -> None:
    """单段只读模块与纯数字 id 结尾都要放行"""
    assert client_mod.CoolapkClient._check_get_url_path("/v6/message-like?uid=1")
    assert client_mod.CoolapkClient._check_get_url_path("/v6/user/12345")


def test_page_size_default_and_env_parse(monkeypatch) -> None:
    # 本机设了 COOLAPK_PAGE_SIZE 时按本机值：硬编码 10 会让已配置环境假红
    assert client_mod.CoolapkClient.PAGE_SIZE == client_mod._env_int("COOLAPK_PAGE_SIZE", 10)
    monkeypatch.setenv("COOLAPK_PAGE_SIZE", "20")
    assert client_mod._env_int("COOLAPK_PAGE_SIZE", 10) == 20
    monkeypatch.setenv("COOLAPK_PAGE_SIZE", "not-a-number")
    assert client_mod._env_int("COOLAPK_PAGE_SIZE", 10) == 10
