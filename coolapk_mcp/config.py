"""配置管理 — 设备码、登录态等持久化存储

登录态解析层（环境变量 COOLAPK_LOGIN_MODE=auto|desktop|native，默认 auto）：
- desktop：只读酷安桌面端 %APPDATA%/com.coolapk.desktop/accounts.json
- native：只读本 MCP 的 ~/.coolapk-mcp/config.json
- auto：优先桌面端，桌面端无有效登录态时回落本机 config.json
"""

import json
import os
from pathlib import Path
from urllib.parse import unquote

CONFIG_DIR = Path.home() / ".coolapk-mcp"
CONFIG_FILE = CONFIG_DIR / "config.json"

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


def _load_desktop_login() -> dict[str, str] | None:
    """读桌面端 accounts.json，取当前账号登录数据；无有效登录态返回 None"""
    if not DESKTOP_ACCOUNTS_FILE.exists():
        return None
    try:
        data = json.loads(DESKTOP_ACCOUNTS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return None

    accounts = data.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        return None

    last_uid = data.get("lastLoginUid")
    account: dict | None = None
    if last_uid:
        for acc in accounts:
            if str(acc.get("uid", "")) == str(last_uid):
                account = acc
                break
    if account is None:
        account = accounts[0]

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
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
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
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(
            json.dumps(
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
            ),
            encoding="utf-8",
        )
