"""HTTP 客户端 + API 封装 — 合并网络层和业务端点"""

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from coolapk_mcp import throttle
from coolapk_mcp.auth.token_v3 import DEFAULT_APP_VERSION, DEFAULT_VERSION_CODE, generate_token_v3
from coolapk_mcp.config import AppConfig, has_valid_sessid


# 登录态失效统一提示（error_type=auth_expired，AI 客户端据此引导用户重登）
AUTH_EXPIRED_MESSAGE = (
    "登录态已失效：若已安装酷安第三方桌面端（github.com/coolapk-desktop/coolapk-desktop），"
    "在桌面端重新登录后直接重试即可（本服务 auto 模式会自动复用其登录态，无需重启）；"
    "或运行 coolapk login --web 重新导入登录 Cookie（也可用 login --sms 短信登录、"
    "login --cookie 手动导入）；若按上述方式重新登录后仍提示失效，请重启 MCP 会话"
)


class CoolapkError(Exception):
    """API 业务错误

    kind: api(服务端业务错误) captcha(风控验证码) network(超时/连接)
          transient(5xx/429等可重试) http(其他HTTP错误) parse(响应解析失败)
    """

    def __init__(self, message: str, error_code: int = -1, kind: str = "api"):
        self.message = message
        self.error_code = error_code
        self.kind = kind
        super().__init__(message)


def _extract_request_hash(html: str) -> str:
    """从登录页 HTML 提取 requestHash，兼容新老两种页面格式。

    - 老格式：requestHash: 'xxxx'
    - 新格式（2026-11 页面改版）：
      var authFlow = createAuthCaptchaFlow('/auth/login?type=mobile', 'xxxx')
      只匹配调用处（两个参数均为引号字符串），函数定义行
      function createAuthCaptchaFlow(url, requestHash) 不会误命中。
    """
    m = re.search(r"requestHash[:\s]+['\"]([^'\"]+)['\"]", html)
    if m:
        return m.group(1)
    m = re.search(
        r"createAuthCaptchaFlow\s*\(\s*['\"][^'\"]+['\"]\s*,\s*['\"]([^'\"]+)['\"]", html
    )
    if m:
        return m.group(1)
    return ""


class CoolapkClient:
    """酷安 API 客户端"""

    BASE_URL = "https://api.coolapk.com"

    # 端点被验证码拦截后的冷却期（秒）：期间不再撞该端点，直接走回落
    CAPTCHA_COOLDOWN = 600.0
    # 详情回落时最多翻作者动态的页数
    FALLBACK_SCAN_PAGES = 3
    # feed_id → 作者uid 索引，持久化到 ~/.coolapk-mcp/uid_index.json
    INDEX_FILE = Path.home() / ".coolapk-mcp" / "uid_index.json"
    INDEX_MAX = 2000

    def __init__(self, config: AppConfig | None = None):
        self.config = config or AppConfig.load()
        try:
            self._client = httpx.AsyncClient(timeout=30.0)
        except httpx.InvalidURL:
            # NO_PROXY 等环境变量含非法值（如 [::1]）时 httpx 会解析失败，禁用环境代理兜底
            self._client = httpx.AsyncClient(timeout=30.0, trust_env=False)
        self._limiter = throttle.RateLimiter()
        self._cache = throttle.TTLCache()
        self._blocked: dict[str, float] = {}
        self._uid_index: dict[int, int] = {}
        self._load_uid_index()

    # ---- feed_id → uid 索引（详情回落的关键） ----

    def _load_uid_index(self) -> None:
        try:
            data = json.loads(self.INDEX_FILE.read_text(encoding="utf-8"))
            # 文件被写坏时可能是 null/list/str，dict 预检防止 AttributeError 崩穿构造
            if not isinstance(data, dict):
                self._uid_index = {}
                return
            self._uid_index = {int(k): int(v) for k, v in data.items()}
        except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
            self._uid_index = {}

    def _save_uid_index(self) -> None:
        try:
            self.INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
            trimmed = dict(list(self._uid_index.items())[-self.INDEX_MAX:])
            # 原子写：先落临时文件再 replace，防止中断产生半截 JSON 被 load 静默清空
            tmp = self.INDEX_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(trimmed), encoding="utf-8")
            os.replace(tmp, self.INDEX_FILE)
        except OSError:
            pass

    def _index_feeds(self, data: Any) -> None:
        """从列表响应记录 feed_id → 作者uid，供详情被风控时回落"""
        if not isinstance(data, list):
            return
        changed = False
        for item in data:
            if not isinstance(item, dict):
                continue
            if item.get("entityType") not in ("feed", None):
                continue
            fid = item.get("id")
            uid = item.get("uid") or (item.get("userInfo") or {}).get("uid")
            if not fid or not uid:
                continue
            try:
                self._uid_index[int(fid)] = int(uid)
                changed = True
            except (TypeError, ValueError):
                continue
        if changed:
            self._save_uid_index()

    def _build_headers(self) -> dict[str, str]:
        """组装请求头，每次请求重新生成 X-App-Token（v3 算法）"""
        token, _ = generate_token_v3(self.config.device_code)
        return {
            "X-Sdk-Int": "35",
            "X-Sdk-Locale": "zh-CN",
            "X-App-Mode": "universal",
            "X-App-Channel": "coolapk",
            "X-App-Id": "com.coolapk.market",
            "X-App-Device": self.config.device_code,
            "X-App-Version": DEFAULT_APP_VERSION,
            "X-App-Code": str(DEFAULT_VERSION_CODE),
            "X-Api-Version": "16",
            "X-App-Supported": str(DEFAULT_VERSION_CODE),
            "X-Dark-Mode": "0",
            "X-Requested-With": "XMLHttpRequest",
            "X-App-Token": token,
            "User-Agent": (
                "Dalvik/2.1.0 (Linux; U; Android 16; 23113RKC6C Build/AQ3A.250226.002) "
                "(#Build; Redmi; 23113RKC6C; AQ3A.250226.002; HyperOS_3.0; 3.0.1.0) "
                f"+CoolMarket/{DEFAULT_APP_VERSION}-{DEFAULT_VERSION_CODE}-universal"
            ),
        }

    def _get_cookie_header(self) -> str | None:
        """返回 Cookie 字符串。直接拼字符串避免 httpx cookies 参数对中文 username 的编码问题。

        桌面端导入的完整 Cookie 串（含 SESSID 等）直接整串使用；
        否则按 APP 端三键（uid/username/token）拼接。
        """
        if not self.config.is_logged_in:
            return None
        if self.config.desktop_cookie and has_valid_sessid(self.config.desktop_cookie):
            return self.config.desktop_cookie
        return f"uid={self.config.uid}; username={quote(self.config.username, safe='')}; token={self.config.token}"

    def _check_local_auth(self) -> None:
        """登录前本地校验：未登录或完整 Cookie 串 SESSID 已失效直接抛 auth_expired，不发探测请求

        桌面端导出串形如 "; SESSID=..."（分号后可能带空格，config.py 同款注释），
        判定统一交给 has_valid_sessid（按键解析、strip 处理），不用子串匹配，
        避免漏检 "; SESSID=deleted" 这类带空格变体；SMS 登录只落三键串（无
        SESSID），不算失效。
        """
        if not self.config.is_logged_in:
            raise CoolapkError(AUTH_EXPIRED_MESSAGE, kind="auth_expired")
        cookie = self.config.desktop_cookie
        if cookie and not has_valid_sessid(cookie) and "sessid=" in cookie.lower():
            raise CoolapkError(AUTH_EXPIRED_MESSAGE, kind="auth_expired")

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        """统一请求方法：本地登录预检 → GET 缓存 → 全局限速 → 瞬态错误退避重试 → 错误分类"""
        # 本地预检：未登录或 SESSID 已失效直接抛 auth_expired，不发探测请求；
        # 请求类端点（登录流程自身）不受此限制
        if not path.startswith("/v6/auth/"):
            self._check_local_auth()

        cache_key = None
        if method == "GET" and throttle.CACHE_ENABLED:
            params = kwargs.get("params") or {}
            cache_key = f"{path}?{sorted((str(k), str(v)) for k, v in params.items())}"
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

        url = f"{self.config.api_base}{path}"
        resp = None
        last_err: CoolapkError | None = None
        last_status: int | None = None
        for attempt in range(throttle.MAX_RETRIES + 1):
            await self._limiter.acquire()
            headers = self._build_headers()
            cookie = self._get_cookie_header()
            if cookie:
                headers["Cookie"] = cookie
            err: CoolapkError | None = None
            try:
                resp = await self._client.request(
                    method, url, headers=headers, **kwargs
                )
                resp.raise_for_status()
                last_err = None  # 本轮成功：清掉早前失败轮标记，避免循环后被误判为重试耗尽
                break
            except httpx.TimeoutException:
                last_status = None
                err = CoolapkError("请求超时", kind="network")
            except httpx.ConnectError:
                last_status = None
                err = CoolapkError("无法连接服务器", kind="network")
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                last_status = code
                if code == 403:
                    # 403 通常是登录态失效（captchaRequired / 会话无效）
                    raise CoolapkError(AUTH_EXPIRED_MESSAGE, error_code=403, kind="auth_expired")
                kind = "transient" if (code >= 500 or code == 429) else "http"
                err = CoolapkError(f"HTTP {code}", kind=kind)
            if err is not None and err.kind not in ("network", "transient"):
                raise err
            last_err = err
            if attempt < throttle.MAX_RETRIES:
                await throttle.backoff_sleep(attempt)

        if last_err is not None:
            # 重试耗尽仍失败：瞬态错误（超时/连接/5xx/429）重试后依旧不成功。
            # raise_for_status 抛错时 resp 已赋值，不能靠 resp is None 判定；
            # 按末次状态码重抛，不再解析可能是 HTML 错误页的 body（否则会被
            # resp.json() 误分类成 kind=parse，丢失可重试语义）
            raise CoolapkError(
                f"{last_err.message}（重试 {throttle.MAX_RETRIES} 次后仍失败）",
                error_code=last_status if last_status is not None else -1,
                kind=last_err.kind,
            )
        if resp is None:
            raise CoolapkError("请求失败", kind="network")

        try:
            result = resp.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            # 风控页/网关错误页常返回 HTML；编码异常按同类处理
            raise CoolapkError("响应解析失败", kind="parse")

        # 检查业务错误。V6 错误响应两种形态：
        # ① 顶层 {error, message, ...}（无 data 键）；
        # ② {status: 非0, message, data: null}（风控/网络环境异常，status 字段必须读，
        #    只看「无 data 键」会把 status=1004/-415 的风控响应当成功，data=None
        #    还会被写进缓存造成缓存投毒）
        status_code = result.get("status") if isinstance(result, dict) else None
        if isinstance(status_code, bool):
            # 布尔 status：False==0 会混进 (0,1) 合法集被当成功；归一成 int，
            # False 视为失败信号（-1）、True 视为成功（1）
            status_code = 1 if status_code else -1
        failed = (
            not isinstance(result, dict)
            or ("data" not in result and "message" in result)
            or (status_code is not None and status_code not in (0, 1, "0", "1"))
        )
        if failed and isinstance(result, dict):
            error_code = result.get("error", -1)
            message = result.get("message", "未知错误")
            if isinstance(error_code, str):
                error_code = int(error_code) if error_code.lstrip("-").isdigit() else -1
            if status_code is not None and status_code not in (0, 1, "0", "1"):
                # status 字段优先：1004=网络环境异常（风控）、-415=设备码被拉黑
                if isinstance(status_code, str) and status_code.lstrip("-").isdigit():
                    status_code = int(status_code)
                if isinstance(status_code, int):
                    error_code = error_code if error_code != -1 else status_code
            # 403/captchaRequired 是登录态失效，必须先于「验证码」判定——
            # 否则 message 里的 captchaRequired 会被 "captcha" 子串抢判成图形验证码
            if error_code == 403 or "captchaRequired" in str(result):
                raise CoolapkError(AUTH_EXPIRED_MESSAGE, error_code=403, kind="auth_expired")
            if "验证码" in message:
                # 负缓存：该端点短期内已被风控，冷却期内不再请求
                base = path.split("?", 1)[0]
                self._blocked[base] = time.monotonic() + self.CAPTCHA_COOLDOWN
                raise CoolapkError(message, error_code, kind="captcha")
            if error_code == -415:
                raise CoolapkError(
                    f"设备码被风控拉黑({message})，请更换登录态设备码", error_code, kind="api",
                )
            if error_code == 1004:
                raise CoolapkError(
                    f"请求被风控拦截({message})，请稍后再试", error_code, kind="api",
                )
            raise CoolapkError(message, error_code)
        if not isinstance(result, dict):
            # 部分接口（如搜索、话题动态）顶层直接返回 list，是预期形态
            data = result
        else:
            data = result.get("data")
        if isinstance(data, list):
            self._index_feeds(data)
        # data 为 None 视为失败残余，不写入缓存，避免风控响应污染缓存
        if cache_key is not None and data is not None:
            self._cache.put(cache_key, data, throttle.ttl_for(path))
        return data

    def _endpoint_blocked(self, base: str) -> bool:
        until = self._blocked.get(base, 0.0)
        if until and time.monotonic() < until:
            return True
        self._blocked.pop(base, None)
        return False

    async def get(self, path: str, params: dict | None = None) -> Any:
        return await self._request("GET", path, params=params)

    # post/post_multipart 仅登录流程（login --sms 等）内部使用，
    # MCP 工具层与 get_url 均不触达，属于客户端必需的底层能力而非写接口暴露
    async def post(self, path: str, data: dict | None = None) -> Any:
        return await self._request("POST", path, data=data or {})

    async def post_multipart(
        self, path: str, fields: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> Any:
        """multipart/form-data POST。files 格式: {name: (filename, content, content_type)}"""
        return await self._request(
            "POST", path, data=fields or {}, files=files or {}
        )

    # ---- 只读透传与图片下载（MCP 工具专用，不用于业务写入） ----

    @classmethod
    def _check_get_url_path(cls, path: str) -> str:
        """校验 get_url 的路径参数：仅允许 api.coolapk.com 的 GET /v6/ 只读路径。

        返回规范化后的路径；违规抛 CoolapkError(error_code=403)。
        """
        path = (path or "").strip()
        # 允许整段 URL 输入，但必须是 api.coolapk.com
        if path.startswith(("http://", "https://")):
            from urllib.parse import urlsplit
            host = urlsplit(path).netloc.lower()
            if host not in ("api.coolapk.com", "api2.coolapk.com"):
                raise CoolapkError(
                    f"拒绝访问非酷安 API 域名: {host}（仅允许 api.coolapk.com）",
                    error_code=403, kind="api",
                )
            path = urlsplit(path).path + (
                f"?{urlsplit(path).query}" if urlsplit(path).query else ""
            )
        if not path.startswith("/"):
            path = f"/{path}"
        if not path.startswith("/v6/"):
            raise CoolapkError(
                f"仅允许 /v6/ 开头的接口路径，收到: {path}",
                error_code=403, kind="api",
            )
        # 写端点黑名单：按「query 之前的路径部分」的独立段匹配，拒绝写动作端点。
        # （酷安写接口全部走 GET 传参，必须拦在客户端；docs/coolapk-api.md:574
        # /v6/feed/like 即 GET。）不能裸子串匹配——/v6/feed/replyList 含 "/reply"、
        # /v6/message-like 含 "/message" 这类只读端点会被误伤；
        # 校验前先循环 unquote 防双重编码绕过（%64igg、%2564igg）。
        from urllib.parse import unquote

        decoded = path
        for _ in range(3):
            unquoted = unquote(decoded)
            if unquoted == decoded:
                break
            decoded = unquoted
        lowered = decoded.lower()
        path_part = lowered.split("?", 1)[0]
        segs = [seg for seg in path_part.strip("/").split("/") if seg]
        # account 模块整组敏感（登录/验证码/token 下发/风控），透传只放行
        # 登录态检查与风控解锁两个端点；内部业务调用不走本校验，不受影响
        if len(segs) >= 2 and segs[0] == "v6" and segs[1] == "account":
            if segs[2:] not in (["checklogininfo"], ["requestvalidate"]):
                raise CoolapkError(
                    f"拒绝访问 account 模块端点: {path}（仅放行 checkLoginInfo/requestValidate）",
                    error_code=403, kind="api",
                )
        # 单段 + 相邻两段拼接两种形态：/v6/reply/add 这类写端点
        # 靠 "/reply/add" 两段项命中，且不会误伤单段只读端点（replyList/message-like）
        single_segments = {f"/{s}" for s in segs}
        pair_segments = {f"/{segs[i]}/{segs[i+1]}" for i in range(len(segs) - 1)}
        # 单段黑名单：确切的写动作词。exact 匹配不伤子串（followtag≠follow、
        # replyList 不含 /reply），但没有同名只读端点的泛词必须排除在单段之外——
        # /vote /collection /favorite /blacklist 都是只读模块名
        # （/v6/vote/commentList、/v6/collection/list、/v6/favorite/list、
        # /v6/user/blackList），/like 撞只读的 /v6/notification/like
        blocked_segments = (
            "/digg", "/replyadd", "/replyaddpush",
            "/follow", "/unfollow", "/followuser", "/unfollowuser",
            "/followtag", "/unfollowtag",
            "/createfeed", "/publish", "/deletefeed", "/deletecomment",
            "/delete", "/edit", "/update", "/sendmessage", "/upload", "/report",
            "/block", "/subscribe", "/checkin", "/sign", "/apply", "/modify", "/set",
            "/unfavorite", "/sendvcode",
            "/addtoblacklist", "/removefromblacklist",
            "/addtoignorelist", "/removefromignorelist",
        )
        for seg in blocked_segments:
            if seg in single_segments:
                raise CoolapkError(
                    f"拒绝写操作端点: {path}",
                    error_code=403, kind="api",
                )
        # 泛词写端点按「资源/动作」相邻两段拦：like/unlike 的宿主资源不枚举全，
        # 只拦已知的写端点形态；/notification/like 这类只读端点不在列，不受影响
        blocked_pairs = (
            "/reply/add", "/reply/delete", "/reply/edit",
            "/message/send", "/message/delete",
            "/feed/like", "/feed/unlike",
            "/reply/like", "/reply/unlike",
            "/collection/like", "/collection/unlike",
            "/feed/vote", "/feed/favorite", "/apk/favorite",
            "/collection/follow", "/collection/unfollow",
        )
        for pair in blocked_pairs:
            if pair in pair_segments:
                raise CoolapkError(
                    f"拒绝写操作端点: {path}",
                    error_code=403, kind="api",
                )
        return path

    async def get_url(self, path: str) -> Any:
        """只读透传：GET 任意 /v6/ 只读路径并返回原始 JSON。

        仅允许 api.coolapk.com 域名；写端点黑名单一律 403 拒绝。
        """
        return await self.get(self._check_get_url_path(path))

    @staticmethod
    def _thumb_fetch_url(url: str) -> str | None:
        """带 @WxH 尺寸后缀的 jpg 返回 .m.jpg 缩略地址，否则 None。

        受 COOLAPK_IMG_THUMBNAIL 开关控制（默认开，"0"/"false" 关）。
        缩略长边约 1080px，仍在主流多模态模型视觉窗口内，读图内容不受影响。
        """
        if os.environ.get("COOLAPK_IMG_THUMBNAIL", "1").lower() in ("0", "false"):
            return None
        path = urlsplit(url).path
        if path.endswith(".jpg") and "@" in path:
            return url + ".m.jpg"
        return None

    async def download_image(self, url: str) -> tuple[bytes, str]:
        """下载 CDN 图片，返回 (图片字节, 格式名)。

        酷安图片 CDN 校验 Referer：必须带 https://www.coolapk.com/，
        用浏览器 UA 即可放行（实测无需 App UA，见 src-tauri client.rs:3790）。
        仅允许 image/avatar/static.coolapk.com 三个 CDN 域名。
        """
        from urllib.parse import urlsplit

        url = (url or "").strip()
        if url.startswith("//"):
            url = f"https:{url}"
        host = urlsplit(url).netloc.lower()
        allowed_hosts = (
            "image.coolapk.com", "avatar.coolapk.com", "static.coolapk.com",
        )
        if host not in allowed_hosts:
            raise CoolapkError(
                f"图片域名不在白名单: {host or url}"
                f"（仅允许 {'/'.join(allowed_hosts)}）",
                error_code=403, kind="api",
            )
        if url.startswith("http://"):
            url = f"https://{url[len('http://'):]}"
        # 缩略优先（与 /img 中转同一开关 COOLAPK_IMG_THUMBNAIL，默认开）：
        # 上游非 200 自动回落原图。
        fetch_url = self._thumb_fetch_url(url) or url
        thumbnail_used = fetch_url != url

        import httpx
        from httpx import HTTPError as _HTTPError
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.coolapk.com/",
        }
        try:
            async with httpx.AsyncClient(
                headers=headers, timeout=30, follow_redirects=True,
            ) as client:
                resp = await client.get(fetch_url)
                if thumbnail_used and resp.status_code != 200:
                    resp = await client.get(url)
                    thumbnail_used = False
        except httpx.InvalidURL:
            # NO_PROXY 等环境变量含非法值（如 [::1]）时解析失败，禁用环境代理兜底
            async with httpx.AsyncClient(
                headers=headers, timeout=30, follow_redirects=True,
                trust_env=False,
            ) as client:
                resp = await client.get(fetch_url)
                if thumbnail_used and resp.status_code != 200:
                    resp = await client.get(url)
                    thumbnail_used = False
        except _HTTPError as exc:
            raise CoolapkError(f"图片下载失败: {exc}", kind="network") from exc

        if resp.status_code != 200:
            raise CoolapkError(
                f"图片下载失败: HTTP {resp.status_code}（URL 可能已失效，"
                f"agent 可改用 coolapk_get_url 获取接口数据，或直接向用户呈现 URL）",
                error_code=resp.status_code, kind="http",
            )
        data = resp.content
        if not data:
            raise CoolapkError("图片下载失败: 响应体为空", kind="http")

        # 从 Content-Type 或 URL 扩展名推断格式（FastMCP Image 需要）。
        # FastMCP 直接 f"image/{format}" 拼 MIME，必须用标准名 "jpeg"，
        # "jpg" 会生成非法的 image/jpg 被客户端拒绝渲染。
        ctype = (resp.headers.get("content-type") or "").lower()
        for fmt in ("webp", "jpeg", "jpg", "png", "gif"):
            if fmt in ctype:
                return data, "jpeg" if fmt in ("jpeg", "jpg") else fmt
        lower_url = url.lower().split("?", 1)[0]
        for ext in (".webp", ".jpg", ".jpeg", ".png", ".gif"):
            if lower_url.endswith(ext):
                return data, "jpeg" if ext in (".jpg", ".jpeg") else ext[1:]
        # 未见扩展名时按魔数判断
        if data.startswith(b"\x89PNG"):
            return data, "png"
        if data.startswith(b"\xff\xd8"):
            return data, "jpeg"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return data, "gif"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return data, "webp"
        # 未知魔数默认 jpeg：HTTP 抓到的图绝大多数是 jpg，png 会生成
        # 错误的 image/png MIME 导致客户端解码失败
        return data, "jpeg"

    # ---- 浏览 ----

    async def get_home_feeds(self, page: int = 1) -> list[dict]:
        """首页推荐动态"""
        return await self.get(
            "/v6/page/dataList",
            {"url": "/page?url=V9_HOME_TAB_FOLLOW", "page": page},
        )

    async def get_hot_feeds(self, page: int = 1) -> list[dict]:
        """热门动态"""
        return await self.get(
            "/v6/page/dataList",
            {"url": "/feed/hotList", "page": page},
        )

    async def get_latest_feeds(self, page: int = 1) -> list[dict]:
        """最新动态"""
        return await self.get(
            "/v6/page/dataList",
            {"url": "/feed/newestList", "page": page},
        )

    async def get_main_feeds(self, page: int = 1) -> list[dict]:
        """首页推荐（main/indexV8）"""
        return await self.get("/v6/main/indexV8", {"page": page})

    async def get_board_feeds(
        self, board_id_or_url: str, page: int = 1
    ) -> list[dict]:
        """通用板块浏览

        Args:
            board_id_or_url: 板块 ID（如 digital）、page_name（如 V10_DIGITAL_HOME）、或完整 url
            page: 页码
        """
        from coolapk_mcp.boards import BOARD_URLS, resolve_board_url

        url = resolve_board_url(board_id_or_url, BOARD_URLS)
        return await self.get("/v6/page/dataList", {"url": url, "page": page})

    async def get_secondhand_feeds(
        self, brand: int | None = None, page: int = 1
    ) -> list[dict]:
        """二手交易帖子

        Args:
            brand: 品牌ID（1005=苹果），None 则全部二手
            page: 页码
        """
        if brand:
            url = f"#/feed/ershouList?brand={brand}&dataListType=staggered"
        else:
            url = "#/feed/ershouList?dataListType=staggered"
        return await self.get("/v6/page/dataList", {"url": url, "page": page})

    # ---- 动态 ----

    async def get_feed_detail(self, feed_id: int) -> dict | list:
        """动态详情"""
        return await self.get(f"/v6/feed/detail?id={feed_id}")

    async def get_feed_replies(
        self, feed_id: int, page: int = 1, list_type: str = "lastupdate_desc"
    ) -> list[dict]:
        """动态回复列表"""
        return await self.get(
            f"/v6/feed/replyList?id={feed_id}&listType={list_type}"
            f"&page={page}&discussMode=1&feedType=feed"
        )

    async def get_hot_replies(self, feed_id: int, page: int = 1) -> list[dict]:
        """热门回复"""
        return await self.get(
            f"/v6/feed/hotReplyList?id={feed_id}&page={page}&discussMode=1"
        )

    async def get_author_replies(self, feed_id: int, page: int = 1) -> list[dict]:
        """只看楼主：拉取楼主在评论区的回复（含楼中楼内联）。

        酷安工具帖的关键补充信息（获取方式、答疑、密码说明）多在楼主回复里，
        fromFeedAuthor=1 能一次精准命中，不用翻全部评论。
        """
        return await self.get(
            f"/v6/feed/replyList?id={feed_id}&listType=&page={page}"
            f"&discussMode=1&feedType=feed&blockStatus=0&fromFeedAuthor=1"
        )

    async def get_popular_replies(self, feed_id: int, page: int = 1) -> list[dict]:
        """热门评论（listType=popular，按点赞排序）。

        高赞评论多为亲测反馈 / 踩坑指南，信息密度远高于默认时间序。
        """
        return await self.get(
            f"/v6/feed/replyList?id={feed_id}&listType=popular&page={page}"
            f"&discussMode=1&feedType=feed&blockStatus=0&fromFeedAuthor=0"
        )

    async def get_sub_replies_paged(
        self, reply_id: int, page: int = 1, max_pages: int = 5
    ) -> list[dict]:
        """楼中楼完整翻页（feedType=feed_reply + firstItem/lastItem 游标）。

        服务端默认只在父评论里内联最近几条楼中楼；作者答疑常在楼中楼深处，
        此方法按游标翻页补全。返回去重后的全部子回复。
        """
        collected: list[dict] = []
        seen: set[str] = set()
        first_item = ""
        last_item = ""
        for _ in range(max(1, max_pages)):
            params = (
                f"/v6/feed/replyList?id={reply_id}&listType=&page={page}"
                f"&discussMode=1&feedType=feed_reply&blockStatus=0&fromFeedAuthor=0"
            )
            if first_item:
                params += f"&firstItem={first_item}"
            if last_item:
                params += f"&lastItem={last_item}"
            batch = await self.get(params)
            if not isinstance(batch, list) or not batch:
                break
            for item in batch:
                if not isinstance(item, dict):
                    continue
                rid = str(item.get("id", ""))
                if not rid or rid in seen:
                    continue
                seen.add(rid)
                collected.append(item)
            first_item = str(batch[0].get("id", "")) if batch else ""
            last_item = str(batch[-1].get("id", "")) if batch else ""
            if len(batch) < 10:
                break
            page += 1
        return collected

    async def get_feed_history(self, feed_id: int, page: int = 1) -> list[dict]:
        """动态修改历史：作者每次编辑正文留下的快照。

        版本考古用——能看到教程帖 v1.0→v2.2 的正文演进（支持机型、获取方式变化）。
        """
        return await self.get(f"/v6/feed/changeHistoryList?id={feed_id}&page={page}")

    async def get_all_replies(
        self, feed_id: int, max_pages: int = 30
    ) -> list[dict]:
        """拉取全部一级评论（自动翻页去重；楼中楼已内联在每条的 replyRows 里）。

        max_pages 是安全上限，防止超大帖（数千评论）把请求预算耗尽；
        超过上限时返回已取到的部分，调用方可对照 replynum 总数判断是否截断。
        默认上限可用环境变量 COOLAPK_MAX_REPLY_PAGES 调整（1-100）。
        """
        if max_pages == 30:
            try:
                env_val = int(os.environ.get("COOLAPK_MAX_REPLY_PAGES", "") or 30)
                max_pages = max(1, min(100, env_val))
            except (TypeError, ValueError):
                pass
        all_replies: list[dict] = []
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            replies = await self.get_feed_replies(feed_id, page)
            if not isinstance(replies, list) or not replies:
                break
            new = 0
            for r in replies:
                if not isinstance(r, dict):
                    continue
                rid = str(r.get("id", ""))
                # 空 rid 无法去重也无法定位，跳过防止重复追加
                if not rid:
                    continue
                if rid in seen:
                    continue
                seen.add(rid)
                all_replies.append(r)
                new += 1
            if new == 0 or len(replies) < 10:
                break
        return all_replies

    async def get_feed_detail_smart(
        self,
        feed_id: int,
        author_uid: int | None = None,
        with_replies: bool = True,
        reply_page: int = 1,
        all_replies: bool = False,
    ) -> dict:
        """动态详情（带风控回落）

        /v6/feed/detail 被验证码拦截时，自动改从作者动态列表取帖子全文；
        回复走独立的 replyList 接口（通常不受详情接口风控影响）。

        Args:
            feed_id: 帖子ID
            author_uid: 作者UID（提供则回落更快；不提供则查本地 feed_id→uid 索引）
            with_replies: 是否带回复
            reply_page: 回复页码

        Returns:
            {"source": "api"|"author_feed_fallback", "data": dict, "replies": list|None}
        """
        detail_raw = None
        source = "api"
        if not self._endpoint_blocked("/v6/feed/detail"):
            try:
                raw = await self.get_feed_detail(feed_id)
                if isinstance(raw, list) and raw:
                    raw = raw[0]
                if isinstance(raw, dict):
                    detail_raw = raw
            except CoolapkError as e:
                if e.kind != "captcha":
                    raise

        if detail_raw is None:
            uid = author_uid or self._uid_index.get(feed_id)
            if not uid:
                raise CoolapkError(
                    "帖子详情被验证码拦截，且无作者UID可回落；请提供 author_uid 或先搜索该帖",
                    kind="captcha",
                )
            for page in range(1, self.FALLBACK_SCAN_PAGES + 1):
                feeds = await self.get_user_feeds(uid, page)
                if not isinstance(feeds, list):
                    break
                for item in feeds:
                    if isinstance(item, dict) and str(item.get("id")) == str(feed_id):
                        detail_raw = item
                        break
                if detail_raw is not None or len(feeds) < 10:
                    break
            if detail_raw is None:
                raise CoolapkError(
                    f"帖子详情被验证码拦截，且在作者(uid={uid})前"
                    f"{self.FALLBACK_SCAN_PAGES}页动态中未找到 id={feed_id}",
                    kind="captcha",
                )
            source = "author_feed_fallback"

        replies = None
        if with_replies:
            try:
                if all_replies:
                    replies = await self.get_all_replies(feed_id)
                else:
                    replies = await self.get_feed_replies(feed_id, reply_page)
            except CoolapkError:
                replies = None  # 回复接口也被拦时降级为只返回正文

        return {"source": source, "data": detail_raw, "replies": replies}

    # ---- 搜索 ----

    async def search(
        self,
        keyword: str,
        search_type: str = "feed",
        page: int = 1,
        sort: str = "default",
    ) -> list[dict]:
        """通用搜索
        search_type: feed / user / feedTopic / app
        sort: feed 专用排序，hot(热门)/reply(回复数)/default(综合)；
            实测 hot 显著提升结果质量（默认排序多为低互动点评）
        """
        params: dict[str, Any] = {
            "type": search_type,
            "searchValue": keyword,
            "page": page,
            "showAnonymous": "-1",
        }
        if search_type == "feed":
            params["sort"] = sort
        return await self.get("/v6/search", params)

    async def search_feeds(
        self,
        keyword: str,
        feed_type: str = "feed",
        sort: str = "default",
        page: int = 1,
    ) -> list[dict]:
        """搜索动态"""
        return await self.get(
            "/v6/search",
            {
                "type": "feed",
                "feedType": feed_type,
                "sort": sort,
                "searchValue": keyword,
                "page": page,
                "showAnonymous": "-1",
            },
        )

    # ---- 用户 ----

    async def get_user_space(self, uid: int) -> dict | list:
        """用户空间"""
        return await self.get(f"/v6/user/space?uid={uid}")

    async def get_user_profile(self, uid: int) -> dict | list:
        """用户资料"""
        return await self.get(f"/v6/user/profile?uid={uid}")

    async def get_user_feeds(
        self, uid: int, page: int = 1, feed_type: str = "feed"
    ) -> list[dict]:
        """用户动态列表"""
        return await self.get(
            f"/v6/user/{feed_type}List?uid={uid}&page={page}&isIncludeTop=1"
        )

    async def get_user_feeds_pages(
        self, uid: int, pages: int = 2, start_page: int = 1, feed_type: str = "feed"
    ) -> list[dict]:
        """用户动态多页聚合，按 id 去重。

        置顶帖带 isIncludeTop=1 时每页都会带一次（实测 3 页 60 条里只有 1 条重复），
        所以去重是必需的。首页失败照常抛错；后续页失败只保留已抓到的部分，
        避免因为翻到底或被限流把整次聚合废掉。
        """
        pages = max(1, min(int(pages or 1), 5))
        start = max(1, int(start_page or 1))
        seen: set[object] = set()
        out: list[dict] = []
        for offset in range(pages):
            page = start + offset
            try:
                items = await self.get_user_feeds(uid, page, feed_type)
            except CoolapkError:
                if offset == 0:
                    raise
                break
            if not isinstance(items, list) or not items:
                break
            fresh = 0
            for item in items:
                if not isinstance(item, dict):
                    continue
                fid = item.get("id")
                if fid is not None:
                    if fid in seen:
                        continue
                    seen.add(fid)
                out.append(item)
                fresh += 1
            if fresh == 0:
                # 整页都是重复，说明已经翻到底（置顶帖重复页）
                break
        return out

    # ---- 话题 ----

    async def get_topic_detail(self, tag: str) -> dict | list:
        """话题详情"""
        # tag 必须显式 quote：话题名常含 #（如 #小米15#），不编码会被
        # httpx 当 fragment 截断，query 实际发送为 tag=（空值）
        return await self.get(f"/v6/topic/newTagDetail?tag={quote(tag, safe='')}")

    async def get_topic_feeds(self, tag: str, page: int = 1) -> list[dict]:
        """话题下的帖子"""
        return await self.get(
            f"/v6/topic/tagFeedList?tag={quote(tag, safe='')}&page={page}"
        )

    async def get_topic_suggest(self, keyword: str) -> list[dict]:
        """搜索联想词（查询改写积木：返回酷安建议的搜索词卡片）"""
        return await self.get(
            f"/v6/search/suggestSearchWordsNew?searchValue={quote(keyword, safe='')}"
        )

    # ---- 应用 ----

    async def get_app_detail(self, package_name: str) -> dict | list:
        """应用详情"""
        return await self.get(f"/v6/apk/detail?id={package_name}")

    # ---- 通知 ----

    async def get_notification_count(self) -> dict | list:
        """未读通知计数"""
        return await self.get("/v6/notification/checkCount")

    async def get_notifications(
        self, ntype: str, page: int = 1
    ) -> list[dict]:
        """通知列表
        ntype: atMeMeFeed / atComment / comment / feedLike / contactsFollow / message
        """
        return await self.get(f"/v6/notification/{ntype}?page={page}")

    # ---- 登录态校验 ----

    async def check_login(self) -> Any:
        """校验当前登录态：本地检查不通过直接抛 auth_expired；通过则调 checkLoginInfo 确认

        返回 {"logged_in": True, "uid": ..., "username": ..., "source": ...}。
        """
        self._check_local_auth()
        await self.get("/v6/account/checkLoginInfo")
        return {
            "logged_in": True,
            "uid": self.config.uid,
            "username": self.config.username,
            "source": self.config.source,
        }

    # ---- 验证码 ----

    async def request_validate(self, captcha: str, **extra) -> Any:
        """提交图形验证码校验（风控解锁）

        Args:
            captcha: 用户输入的验证码
            **extra: 额外参数（视风控场景而定）
        """
        data = {"captcha": captcha, **extra}
        return await self.post("/v6/account/requestValidate", data)

    # ---- SMS 登录（走 account.coolapk.com 网页登录流程） ----

    async def _auth_request(
        self, method: str, path: str, sessid: str = "",
        data: dict | None = None, referer: str = "https://account.coolapk.com/auth/login?type=mobile",
    ) -> httpx.Response:
        """account.coolapk.com 的请求（模拟酷安 APP WebView）

        关键点：
        - User-Agent 用真实 Android WebView UA（Chrome 149）
        - X-Requested-With: com.coolapk.market 标识 APP
        - Cookie 带 DID/oaid/AppSupported 等 APP 设备标识（服务端校验是否来自 APP）
        - 不自动跟随重定向（follow_redirects=False），调用方需自行处理 302
        """
        url = f"https://account.coolapk.com{path}"
        # 组装 Cookie：SESSID + APP 设备标识
        cookie_parts = []
        if sessid:
            cookie_parts.append(sessid)
        cookie_parts.append("forward=https://www.coolapk.com")
        cookie_parts.append("AppSupported=2604201")
        cookie_parts.append("darkMode=0")
        cookie_parts.append("displayVersion=v14")
        # DID 和 oaid 是 APP 设备标识，validateLogin 端点用这些判断是否来自 APP
        # 这些值从手机 ADB 提取或用任意值（服务端不严格校验具体值，只检查字段存在）
        cookie_parts.append("DID=coolapk_mcp_default_did")
        cookie_parts.append("oaid=coolapk_mcp_default_oaid")
        cookie_str = "; ".join(cookie_parts)

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 16; 23113RKC6C Build/BP2A.250605.031.A3; wv) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
                "Chrome/149.0.7827.91 Mobile Safari/537.36"
            ),
            "X-Requested-With": "com.coolapk.market",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": referer,
            "sec-ch-ua": '"Android WebView";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
            "sec-ch-ua-mobile": "?1",
            "sec-ch-ua-platform": '"Android"',
            "Cookie": cookie_str,
        }
        if method == "POST":
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Origin"] = "https://account.coolapk.com"
            headers["X-Requested-With"] = "XMLHttpRequest"
            headers["Accept"] = "*/*"
            headers["Sec-Fetch-Site"] = "same-origin"
            headers["Sec-Fetch-Mode"] = "cors"
            headers["Sec-Fetch-Dest"] = "empty"
        else:
            headers["Upgrade-Insecure-Requests"] = "1"
            headers["Sec-Fetch-Site"] = "none"
            headers["Sec-Fetch-Mode"] = "navigate"
            headers["Sec-Fetch-User"] = "?1"
            headers["Sec-Fetch-Dest"] = "document"
        # 不跟随重定向，让调用方处理 302
        return await self._client.request(method, url, headers=headers, data=data, follow_redirects=False)

    async def get_sms_login_page(self) -> tuple[str, str]:
        """获取 SMS 登录页，返回 (sessid_cookie, request_hash)

        流程：
        1. GET /auth/login?type=mobile 获取 HTML 和 SESSID cookie
        2. 从 HTML 解析 requestHash
        """
        resp = await self._auth_request("GET", "/auth/login?type=mobile")
        html = resp.text
        # 提取 SESSID
        sessid = ""
        for cookie in resp.headers.get_list("set-cookie"):
            if "SESSID=" in cookie:
                sessid = cookie.split(";")[0]
                break
        # 提取 requestHash（兼容新老页面格式，见 _extract_request_hash）
        request_hash = _extract_request_hash(html)
        if not request_hash:
            raise CoolapkError(
                "无法从登录页解析 requestHash（登录页可能又改版了，请到 "
                "https://github.com/yufeng-liang/coolapk-mcp-pro/issues 反馈）"
            )
        return sessid, request_hash

    async def get_captcha_image(self, sessid: str) -> bytes:
        """获取图形验证码图片 bytes

        Args:
            sessid: 从 get_sms_login_page 获取的 SESSID cookie
        """
        import time as _time

        ts = int(_time.time() * 1000)
        resp = await self._auth_request(
            "GET", f"/auth/showCaptchaImage?{ts}", sessid=sessid,
            referer="https://account.coolapk.com/auth/loginByCoolapk",
        )
        return resp.content

    async def send_sms_code(
        self, sessid: str, request_hash: str, phone: str, captcha: str, country: int = 86,
    ) -> dict:
        """发送短信验证码（POST /auth/login?type=mobile）

        这是 SMS 登录的第一步。服务端验证图形验证码后发送短信到手机。

        Args:
            sessid: SESSID cookie
            request_hash: 从登录页获取的 requestHash
            phone: 手机号
            captcha: 图形验证码（用户识别 get_captcha_image 返回的图片后输入）
            country: 国家码，默认 86

        Returns:
            API 响应 dict。
            - HTTP 302：验证码正确，短信已发送。Location 头是验证码输入页 URL。
              返回 {"status": 1, "validate_url": "<Location>", "message": "短信验证码已发送"}
            - HTTP 200 + JSON：验证码错误或频率限制。看 message 字段。
            - status==-1 表示需要图形验证码。
        """
        import time as _time

        data = {
            "submit": 1,
            "requestHash": request_hash,
            "country": country,
            "mobile": phone,
            "captcha": captcha,
            "randomNumber": f"{int(_time.time()*1000)}{int(_time.time()%1000)}",
            # 网易易盾反机器人 token：nscode=200 是固定成功码，
            # nstoken 用任意值即可通过服务端 APP 环境检测（实测验证）
            "nscode": "200",
            "nstoken": "coolapk_mcp_python_client",
        }
        resp = await self._auth_request("POST", "/auth/login?type=mobile", sessid=sessid, data=data)

        # 302 重定向 = 验证码正确，短信已发送
        if resp.status_code == 302:
            location = resp.headers.get("location", "")
            return {
                "status": 1,
                "message": "短信验证码已发送",
                "validate_url": location,
                "sessid": sessid,
            }

        # 200 JSON 响应
        try:
            result = resp.json()
        except Exception:
            text = resp.text[:500] if resp.text else ""
            raise CoolapkError(f"发送验证码响应解析失败: HTTP {resp.status_code}, body={text}")
        return result

    async def login_sms(self, sessid: str, validate_url: str, code: str) -> dict:
        """提交短信验证码完成登录

        这是 SMS 登录的第二步。send_sms_code 成功后返回的 validate_url 是
        `/auth/validateLogin?key=<token>` 格式。

        流程：
        1. GET validate_url 获取验证码输入页 HTML + 新的 requestHash
        2. POST validate_url 提交 code + type=code + requestHash

        Args:
            sessid: SESSID cookie（必须与 send_sms_code 用同一个 session）
            validate_url: send_sms_code 成功返回的 validate_url
            code: 用户收到的短信验证码

        Returns:
            API 响应 dict。成功后 Set-Cookie 里会有 uid/username/token。
        """
        import time as _time

        # validate_url 是路径格式 /auth/validateLogin?key=xxx
        path = validate_url
        if not validate_url.startswith("/"):
            # 整段 URL 输入时剥掉 host 部分
            for host in ("https://account.coolapk.com", "https://www.coolapk.com"):
                if validate_url.startswith(host):
                    path = validate_url[len(host):]
                    break
            else:
                path = f"/{validate_url}"

        # 1. GET validate 页面获取新的 requestHash
        resp = await self._auth_request("GET", path, sessid=sessid)
        html = resp.text
        request_hash = _extract_request_hash(html)

        # 2. POST 提交验证码
        data = {
            "submit": 1,
            "requestHash": request_hash,
            "code": code,
            "type": "code",
            "randomNumber": f"{int(_time.time()*1000)}{int(_time.time()%1000)}",
            # 网易易盾反机器人 token（同 send_sms_code）
            "nscode": "200",
            "nstoken": "coolapk_mcp_python_client",
        }
        resp = await self._auth_request(
            "POST", path, sessid=sessid, data=data,
            referer=f"https://account.coolapk.com{path}",
        )

        # 检查 302 重定向（登录成功）
        if resp.status_code == 302:
            location = resp.headers.get("location", "")
            cookies = {}
            for cookie in resp.headers.get_list("set-cookie"):
                for name in ("uid", "username", "token"):
                    if cookie.startswith(f"{name}="):
                        val = cookie.split(";")[0].split("=", 1)[1]
                        if val and val != "deleted":
                            cookies[name] = val
            return {
                "status": 1,
                "message": "登录成功",
                "redirect": location,
                "_cookies": cookies,
            }

        try:
            result = resp.json()
        except Exception:
            text = resp.text[:500] if resp.text else ""
            raise CoolapkError(f"短信验证码登录响应解析失败: HTTP {resp.status_code}, body={text}")

        # 从 Set-Cookie 提取 uid/username/token
        cookies = {}
        for cookie in resp.headers.get_list("set-cookie"):
            for name in ("uid", "username", "token"):
                if cookie.startswith(f"{name}="):
                    val = cookie.split(";")[0].split("=", 1)[1]
                    if val and val != "deleted":
                        cookies[name] = val
        result["_cookies"] = cookies
        return result

    async def close(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()
