"""浏览器 CDP 登录 —— 不依赖 pythonnet / pywebview

用本机已安装的 Chrome 或 Edge 启动一个独立 profile 的浏览器窗口，用户在其中
正常登录酷安后，通过 Chrome DevTools Protocol 的 ``Storage.getCookies`` 读回
coolapk.com 的 Cookie。

这样做的原因：pywebview 在 Windows 上的后端依赖 pythonnet，而 pythonnet 至今
没有发布支持 CPython 3.14 的版本（pythonnet/pythonnet#2610），所以 Python 3.14
下 ``import clr`` 会直接抛 RuntimeError。走系统浏览器 + CDP 只依赖标准库
（外加纯 Python 的 websocket-client），任何 Python 版本都能用。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

# 需要读取 Cookie 的域名后缀（account.coolapk.com 与 www.coolapk.com 都命中）
COOKIE_DOMAIN_SUFFIXES = ("coolapk.com",)

# SESSID 出现这些取值表示登录态已失效，不能算登录成功
INVALID_SESSID = ("deleted", "expired")

# 从启动浏览器到 DevTools 就绪的最长等待（秒）
_DEVTOOLS_START_TIMEOUT = 20.0

# 轮询 Cookie 的间隔（秒）
_POLL_INTERVAL = 1.0


class BrowserNotFound(RuntimeError):
    """本机找不到可用的 Chrome / Edge"""


def _free_port() -> int:
    """取一个当前空闲的本地端口给 --remote-debugging-port 用"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _candidate_browsers() -> list[Path]:
    """按常见安装位置列出可用的 Chromium 系浏览器可执行文件"""
    if sys.platform == "win32":
        roots = [
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        rels = [
            r"Google\Chrome\Application\chrome.exe",
            r"Microsoft\Edge\Application\msedge.exe",
            r"Chrome\Application\chrome.exe",
            r"Microsoft\Edge\Application\msedge.exe",
        ]
        paths = [Path(root) / rel for root in roots if root for rel in rels]
    elif sys.platform == "darwin":
        paths = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    else:
        paths = [
            Path(name)
            for name in (
                "google-chrome",
                "google-chrome-stable",
                "chromium",
                "chromium-browser",
                "microsoft-edge",
                "microsoft-edge-stable",
            )
        ]
    return paths


def find_browser() -> Path | None:
    """找可用的 Chromium 系浏览器；可用环境变量 COOLAPK_BROWSER 直接指定"""
    override = os.environ.get("COOLAPK_BROWSER", "").strip()
    if override:
        path = Path(override).expanduser()
        if path.exists():
            return path
        raise BrowserNotFound(f"COOLAPK_BROWSER 指向的文件不存在: {path}")

    for path in _candidate_browsers():
        if path.is_absolute():
            if path.exists():
                return path
            continue
        found = shutil.which(str(path))
        if found:
            return Path(found)
    return None


def _devtools_ws_url(port: int) -> str | None:
    """读取 /json/version 拿浏览器级 WebSocket 地址；没就绪返回 None"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None
    url = payload.get("webSocketDebuggerUrl") or ""
    return url or None


def _read_cookies(ws) -> list[dict]:
    """通过 CDP 的 Storage.getCookies 读取浏览器全部 Cookie"""
    ws.send(json.dumps({"id": 1, "method": "Storage.getCookies", "params": {}}))
    deadline = time.time() + 5
    while time.time() < deadline:
        message = json.loads(ws.recv())
        if message.get("id") != 1:
            continue
        if "error" in message:
            raise RuntimeError(f"CDP Storage.getCookies 失败: {message['error']}")
        return list((message.get("result") or {}).get("cookies") or [])
    raise RuntimeError("CDP Storage.getCookies 超时")


def _merge_cookies(jar: dict[str, str], cookies: list[dict]) -> None:
    """把 CDP 返回的 Cookie 合并进扁平字典（同名以更具体的域为准）"""
    best_domain: dict[str, str] = {}
    for item in cookies:
        name = (item.get("name") or "").strip()
        value = item.get("value") or ""
        domain = (item.get("domain") or "").lstrip(".")
        if not name or not domain:
            continue
        if not any(domain == suffix or domain.endswith("." + suffix) for suffix in COOKIE_DOMAIN_SUFFIXES):
            continue
        # 域名越长越具体（account.coolapk.com 优先于 .coolapk.com）
        if name in jar and len(best_domain.get(name, "")) > len(domain):
            continue
        jar[name] = value
        best_domain[name] = domain


def _jar_ready(jar: dict[str, str]) -> bool:
    """判断 Cookie 是否已经能构成一份可用的登录态"""
    sessid = (jar.get("SESSID") or "").strip()
    return bool(
        sessid
        and sessid.lower() not in INVALID_SESSID
        and jar.get("uid")
        and jar.get("token")
    )


def _login_on_port(port: int, url: str, timeout: float = 300) -> tuple[str | None, str]:
    """连接已在运行的浏览器（COOLAPK_CDP_PORT），等待用户完成登录"""
    try:
        import websocket  # type: ignore
    except Exception:
        return None, "no_browser"

    ws_url = ""
    deadline = time.time() + _DEVTOOLS_START_TIMEOUT
    while time.time() < deadline:
        ws_url = _devtools_ws_url(port) or ""
        if ws_url:
            break
        time.sleep(0.3)
    if not ws_url:
        return None, "launch_failed"

    ws = websocket.create_connection(ws_url, timeout=10)
    jar: dict[str, str] = {}
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                _merge_cookies(jar, _read_cookies(ws))
            except Exception:
                pass
            if _jar_ready(jar):
                return "; ".join(f"{key}={value}" for key, value in jar.items()), "ok"
            time.sleep(_POLL_INTERVAL)
        return None, "timeout"
    finally:
        try:
            ws.close()
        except Exception:
            pass


def run_browser_login(url: str, timeout: float = 300) -> tuple[str | None, str]:
    """打开系统浏览器让用户登录，返回 (cookie 串, 状态)

    状态取值：
    - ``ok``：拿到完整 Cookie 串
    - ``timeout``：浏览器开着但超时前没完成登录
    - ``closed``：用户提前关掉了浏览器窗口
    - ``no_browser``：本机没有 Chrome / Edge（调用方可回退其它登录方式）
    - ``launch_failed``：浏览器进程起不来（受限环境），需人工介入

    环境变量 ``COOLAPK_CDP_PORT`` 存在时不再启动浏览器，直接连接该端口上
    已在运行的 Chrome/Edge（该实例需带 ``--remote-debugging-port`` 启动）。
    """
    browser = find_browser()
    if browser is None:
        return None, "no_browser"

    try:
        import websocket  # type: ignore
    except Exception:
        return None, "no_browser"

    attach_port = os.environ.get("COOLAPK_CDP_PORT", "").strip()
    if attach_port.isdigit():
        return _login_on_port(int(attach_port), url, timeout)

    port = _free_port()
    profile_dir = Path(tempfile.mkdtemp(prefix="coolapk-login-"))
    args = [
        str(browser),
        f"--remote-debugging-port={port}",
        # Chrome 111+ 起，非浏览器来源的 CDP WebSocket 需要显式放行
        "--remote-allow-origins=*",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-extensions",
        "--new-window",
        url,
    ]

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )

    ws_url = ""
    start_deadline = time.time() + _DEVTOOLS_START_TIMEOUT
    launched_at = time.time()
    try:
        while time.time() < start_deadline:
            if proc.poll() is not None:
                # 刚启动就退出：多半是受限环境里浏览器无法存活
                return None, "launch_failed" if time.time() - launched_at < 5 else "closed"
            ws_url = _devtools_ws_url(port) or ""
            if ws_url:
                break
            time.sleep(0.3)

        if not ws_url:
            return None, "timeout"

        ws = websocket.create_connection(ws_url, timeout=10)
        jar: dict[str, str] = {}
        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                if proc.poll() is not None:
                    return None, "closed"
                try:
                    _merge_cookies(jar, _read_cookies(ws))
                except Exception:
                    # 浏览器正在导航（如 webSocket 换页）时读失败属正常，下一轮重试
                    pass
                if _jar_ready(jar):
                    return "; ".join(f"{key}={value}" for key, value in jar.items()), "ok"
                time.sleep(_POLL_INTERVAL)
        finally:
            try:
                ws.close()
            except Exception:
                pass
        return None, "timeout"
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        # 临时 profile 里存着登录态，必须删掉
        shutil.rmtree(profile_dir, ignore_errors=True)
