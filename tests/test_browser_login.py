"""浏览器登录的进程/端口编排测试（不启动真浏览器）"""
from __future__ import annotations

import types
from pathlib import Path

from coolapk_mcp import browser_login as bl


class _FakeProc:
    def __init__(self, alive: bool = True):
        self.alive = alive
        self.terminated = 0
        self.killed = 0

    def poll(self):
        return None if self.alive else 1

    def terminate(self):
        self.terminated += 1

    def kill(self):
        self.killed += 1
        self.alive = False

    def wait(self, timeout=None):
        self.alive = False
        return 0


def test_dispose_removes_profile_and_kills_when_terminate_fails(tmp_path):
    """收尸要删掉带登录态的临时 profile，terminate 失败也要兜到 kill"""
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "Cookies").write_text("secret", encoding="utf-8")

    class _Stubborn(_FakeProc):
        def terminate(self):
            raise RuntimeError("already gone")

    proc = _Stubborn(alive=True)
    bl._dispose(proc, profile)

    assert profile.exists() is False
    assert proc.killed == 1


def _patch_spawn_env(monkeypatch, tmp_path, proc, ws_url):
    monkeypatch.setattr(bl, "_free_port", lambda: 12345)
    monkeypatch.setattr(bl.tempfile, "mkdtemp", lambda prefix="": str(tmp_path / "prof"))
    monkeypatch.setattr(bl.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(bl, "_devtools_ws_url", lambda port: ws_url)


def test_spawn_and_wait_returns_ws_url(monkeypatch, tmp_path):
    """正常路径：端口就绪即返回 ws 地址，failure 为空"""
    proc = _FakeProc()
    _patch_spawn_env(monkeypatch, tmp_path, proc, "ws://127.0.0.1:12345/devtools/page/1")
    got_proc, port, _profile, ws_url, failure = bl._spawn_and_wait(Path("chrome"), "about:blank")

    assert got_proc is proc
    assert port == 12345
    assert ws_url.endswith("/devtools/page/1")
    assert failure == ""


def test_spawn_and_wait_reports_early_exit(monkeypatch, tmp_path):
    """进程刚起就没了：分类成 exited_early，调用方映射为 launch_failed"""
    _patch_spawn_env(monkeypatch, tmp_path, _FakeProc(alive=False), "")
    _proc, _port, _profile, ws_url, failure = bl._spawn_and_wait(Path("chrome"), "about:blank")
    assert ws_url == ""
    assert failure == "exited_early"


def test_spawn_and_wait_reports_no_cdp(monkeypatch, tmp_path):
    """进程活着但调试口始终不通：判 no_cdp（这才是端口竞态的典型症状）"""
    proc = _FakeProc()
    _patch_spawn_env(monkeypatch, tmp_path, proc, "")

    # 每读一次时间跳 100s：循环一轮就超时，不真睡 0.3s×N
    ticks = iter(range(0, 1_000_000, 100))
    monkeypatch.setattr(bl.time, "time", lambda: float(next(ticks)))

    _proc, _port, _profile, ws_url, failure = bl._spawn_and_wait(Path("chrome"), "about:blank")
    assert ws_url == ""
    assert failure == "no_cdp"


def test_run_browser_login_retries_once_after_port_race(monkeypatch, tmp_path):
    """第一次端口被抢（no_cdp）→ 换端口重试成功，旧 profile 已清理"""
    attempts: list[str] = []

    def fake_spawn(browser, url):
        attempts.append("spawn")
        profile = tmp_path / f"prof{len(attempts)}"
        profile.mkdir()
        if len(attempts) == 1:
            return _FakeProc(), 1111, profile, "", "no_cdp"
        return _FakeProc(), 2222, profile, "ws://127.0.0.1:2222/devtools/page/1", ""

    monkeypatch.setattr(bl, "find_browser", lambda: Path("chrome"))
    monkeypatch.setattr(bl, "_spawn_and_wait", fake_spawn)

    class _Ws:
        closed = False

        def close(self):
            self.closed = True

    fake_ws_mod = types.SimpleNamespace(create_connection=lambda url, timeout=0: _Ws())
    monkeypatch.setitem(__import__("sys").modules, "websocket", fake_ws_mod)
    # CDP 形状：必须是带 name/value/domain 的列表，且域要落在 COOKIE_DOMAIN_SUFFIXES 内
    monkeypatch.setattr(
        bl,
        "_read_cookies",
        lambda ws: [
            {"name": "uid", "value": "1", "domain": ".coolapk.com"},
            {"name": "token", "value": "t", "domain": ".coolapk.com"},
            {"name": "SESSID", "value": "s", "domain": "account.coolapk.com"},
        ],
    )

    cookie, status = bl.run_browser_login("https://www.coolapk.com", timeout=1)

    assert status == "ok"
    assert "uid=1" in (cookie or "")
    assert len(attempts) == 2
    assert (tmp_path / "prof1").exists() is False
