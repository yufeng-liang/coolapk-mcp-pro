"""配置管理 — 设备码、登录态等持久化存储

登录态解析层（环境变量 COOLAPK_LOGIN_MODE=auto|desktop|native，默认 auto）：
- desktop：只读酷安桌面端 %APPDATA%/com.coolapk.desktop/accounts.json
- native：只读本 MCP 的 ~/.coolapk-mcp/config.json
- auto：优先桌面端，桌面端无有效登录态时回落本机 config.json
"""

import json
import os
import subprocess
from pathlib import Path
from urllib.parse import unquote

CONFIG_DIR = Path.home() / ".coolapk-mcp"
CONFIG_FILE = CONFIG_DIR / "config.json"


def restrict_to_owner(path: Path, mode: int = 0o600) -> None:
    """把含凭据的文件收紧到仅当前用户可读写。

    POSIX 用 chmod（0600）；Windows 的 os.chmod 只能改只读位，必须走 icacls：
    先 /inheritance:r 去掉继承来的 ACE，再只授权当前用户完全控制。
    任何失败都只忽略——权限收紧不该让登录态保存或启动失败。
    """
    try:
        if os.name == "posix":
            os.chmod(path, mode)
            return
        user = os.environ.get("USERNAME") or os.environ.get("USER")
        if not user:
            return
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass

# 酷安桌面端登录数据目录（Windows %APPDATA%）
DESKTOP_CONFIG_DIR = (
    Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    / "com.coolapk.desktop"
)
DESKTOP_ACCOUNTS_FILE = DESKTOP_CONFIG_DIR / "accounts.json"

# SESSID 无效值判定，与桌面端 has_valid_session_cookie 实现保持一致
_INVALID_SESSID_VALUES = {"deleted", "expired"}

LOGIN_MODE_ENV = "COOLAPK_LOGIN_MODE"

# 登录态来源
SOURCE_DESKTOP = "desktop"
SOURCE_NATIVE = "native"
SOURCE_NONE = "none"


def get_login_mode() -> str:
    """登录态解析模式：env 可锁定，缺省 auto；非法值按 auto 处理"""
    mode = os.environ.get(LOGIN_MODE_ENV, "auto").strip().lower()
    return mode if mode in ("auto", "desktop", "native") else "auto"


def parse_cookie_str(cookie_str: str) -> dict[str, str]:
    """解析 Cookie 串为 dict；键值均 strip（桌面端导出串形如 "; SESSID=..."）"""
    cookies: dict[str, str] = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        if key:
            cookies[key.strip()] = value.strip()
    return cookies


def has_valid_sessid(cookie_str: str) -> bool:
    """Cookie 串是否含有效 SESSID（非空且非 deleted/expired，忽略大小写）"""
    for part in cookie_str.split(";"):
        key, _, value = part.partition("=")
        if key.strip() == "SESSID":
            sessid = value.strip()
            return bool(sessid) and sessid.lower() not in _INVALID_SESSID_VALUES
    return False


_JSON_CACHE: dict[str, tuple[tuple[int, int], object]] = {}


def _read_json_cached(path: Path) -> object:
    """按 (mtime_ns, size) 复用 JSON 解析结果。

    AppConfig.load() 每次工具调用都会读一遍 config.json 与桌面端 accounts.json；
    解析结果只随内容变化，按签名缓存可以省掉重复 IO 与解析，同时保留「改文件 →
    下一次调用即生效」的热加载语义（签名一变就重读）。
    """
    try:
        st = path.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        _JSON_CACHE.pop(str(path), None)
        raise
    cached = _JSON_CACHE.get(str(path))
    if cached is not None and cached[0] == sig:
        return cached[1]
    data = json.loads(path.read_text(encoding="utf-8"))
    _JSON_CACHE[str(path)] = (sig, data)
    return data


def _load_desktop_login() -> dict[str, str] | None:
    """读桌面端 accounts.json，取当前账号登录数据；无有效登录态返回 None"""
    if not DESKTOP_ACCOUNTS_FILE.exists():
        return None
    try:
        data = _read_json_cached(DESKTOP_ACCOUNTS_FILE)
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    # 桌面端文件被写坏时可能是 null/list/str 等非法结构，不能让 AttributeError 崩穿 load()
    if not isinstance(data, dict):
        return None

    accounts = data.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        return None

    last_uid = data.get("lastLoginUid")
    account: dict | None = None
    if last_uid:
        for acc in accounts:
            if not isinstance(acc, dict):
                continue
            if str(acc.get("uid", "")) == str(last_uid):
                account = acc
                break
    if account is None:
        account = next((acc for acc in accounts if isinstance(acc, dict)), None)
    if not isinstance(account, dict):
        return None

    cookie = str(account.get("cookie", "") or "")
    if not has_valid_sessid(cookie):
        return None

    cookies = parse_cookie_str(cookie)
    uid = cookies.get("uid") or str(account.get("uid", "") or "")
    token = cookies.get("token") or ""
    if not (uid and token):
        return None
    # cookie 内的 username 是编码形态，先解码，拼 header 时由 client 统一再编码
    username = unquote(cookies.get("username") or str(account.get("username", "") or ""))

    return {
        "uid": uid,
        "username": username,
        "token": token,
        "cookie": cookie,
        "device_code": str(account.get("deviceCode", "") or ""),
        "device_id": str(account.get("deviceId", "") or ""),
    }


class AppConfig:
    """应用配置，持久化到 ~/.coolapk-mcp/config.json"""

    def __init__(self):
        self.device_code: str = ""
        self.device_id: str = ""  # 桌面端 deviceId（桌面登录态透传，可空）
        self.uid: str = ""
        self.username: str = ""
        self.token: str = ""  # 登录后的 cookie token
        self.api_base: str = "https://api.coolapk.com"
        self.desktop_cookie: str = ""  # 完整登录 Cookie 串（--web/--cookie 导入）
        self.source: str = SOURCE_NONE  # 当前生效登录态来源 desktop|native|none

    @property
    def is_logged_in(self) -> bool:
        return bool(self.uid and self.token)

    @property
    def login_mode(self) -> str:
        return get_login_mode()

    def get_cookies(self) -> dict[str, str]:
        if not self.is_logged_in:
            return {}
        return {"uid": self.uid, "username": self.username, "token": self.token}

    def _apply_native(self) -> None:
        """native 来源生效：完整 Cookie 串优先（SESSID 有效时），否则回落三键"""
        if self.desktop_cookie and has_valid_sessid(self.desktop_cookie):
            cookies = parse_cookie_str(self.desktop_cookie)
            self.uid = cookies.get("uid") or self.uid
            self.token = cookies.get("token") or self.token
            self.username = unquote(cookies.get("username") or self.username)
        self.source = SOURCE_NATIVE if self.is_logged_in else SOURCE_NONE

    @classmethod
    def load(cls) -> "AppConfig":
        """加载配置并按 COOLAPK_LOGIN_MODE 解析登录态"""
        config = cls()
        if CONFIG_FILE.exists():
            try:
                data = _read_json_cached(CONFIG_FILE)
                # 配置被写坏时可能是 null/list/str，dict 预防防止 AttributeError 崩穿 load()
                if not isinstance(data, dict):
                    data = {}
                config.device_code = str(data.get("device_code", "") or "")
                config.uid = str(data.get("uid", "") or "")
                config.username = str(data.get("username", "") or "")
                config.token = str(data.get("token", "") or "")
                config.api_base = data.get("api_base", "https://api.coolapk.com")
                config.desktop_cookie = str(data.get("desktop_cookie", "") or "")
            except (json.JSONDecodeError, KeyError, OSError, ValueError):
                pass

        mode = get_login_mode()
        if mode in ("auto", "desktop"):
            desktop = _load_desktop_login()
            if desktop is not None:
                config.uid = desktop["uid"]
                config.username = desktop["username"]
                config.token = desktop["token"]
                config.desktop_cookie = desktop["cookie"]
                # 设备码顺序：桌面端 deviceCode（已编码形态）优先，空则保留本机值
                if desktop["device_code"]:
                    config.device_code = desktop["device_code"]
                config.device_id = desktop["device_id"]
                config.source = SOURCE_DESKTOP
            elif mode == "desktop":
                # 锁定 desktop 但桌面端无有效登录态：不回落，视为未登录
                config.uid = config.username = config.token = ""
                config.source = SOURCE_NONE
            else:
                config._apply_native()
        else:
            config._apply_native()

        if not config.device_code:
            from coolapk_mcp.auth.device import generate_device_code

            config.device_code = generate_device_code()
            config.save()

        return config

    def save(self) -> None:
        """原子写：先落临时文件再 replace，防止并发/中断产生半截 JSON"""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            # 目录也收成 0700：config.json 本身 0600 了，但目录可列会暴露文件名与备份
            try:
                os.chmod(CONFIG_DIR, 0o700)
            except OSError:
                pass
        payload = json.dumps(
            {
                "device_code": self.device_code,
                "uid": self.uid,
                "username": self.username,
                "token": self.token,
                "api_base": self.api_base,
                "desktop_cookie": self.desktop_cookie,
            },
            ensure_ascii=False,
            indent=2,
        )
        tmp = CONFIG_FILE.with_suffix(".json.tmp")
        if os.name == "posix":
            # 先按 0600 建文件再写：write_text 沿用 umask（常见 022 → 0644），
            # 在下面的 chmod 之前存在一段「token/SESSID 全局可读」的窗口
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
        else:
            tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, CONFIG_FILE)
        # 配置里有 uid/token/完整 Cookie：POSIX 收成 0600、Windows 用 icacls
        # 去掉继承 ACE 只留当前用户（os.chmod 在 Windows 只能改只读位，形同无效）
        restrict_to_owner(CONFIG_FILE)
