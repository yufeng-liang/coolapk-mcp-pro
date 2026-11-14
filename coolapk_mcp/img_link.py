"""酷安图片外链签名：/img 端点、coolapk_image_link 工具与 CLI image-link 共用一份实现。

三处各写一遍 HMAC 拼接是漂移的温床：只要有一处改了拼接顺序或截断长度，
生成的外链就会稳定 403，而错误现象看起来像"密钥不对"。所以只留这一份。
"""
from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import quote, urlsplit

# 只中转酷安自家图片域名，避免 /img 被当成任意 URL 的开放代理
IMAGE_HOSTS = frozenset({"image.coolapk.com", "avatar.coolapk.com", "static.coolapk.com"})
DEFAULT_TTL_MINUTES = 120
MAX_TTL_MINUTES = 10080
_HOST_ERROR = "只支持酷安图片域名（image/avatar/static.coolapk.com）"


def clamp_ttl(minutes: object) -> int:
    """把 ttl 夹到 [1, 10080] 分钟；非法值回落默认 120。

    OverflowError 也要接：float('inf') 能解析成功、int(inf) 才抛错，
    只接 TypeError/ValueError 会让 inf 一路穿到调用方。
    bool 显式回落默认值（str(True) 解析失败也会落回默认，写明避免误读成 1 分钟）。
    """
    if isinstance(minutes, bool):
        return DEFAULT_TTL_MINUTES
    try:
        ttl = int(float(str(minutes)))
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_TTL_MINUTES
    return max(1, min(ttl, MAX_TTL_MINUTES))


def sign_image(raw: str, secret: str, exp: int) -> str:
    """对 url|exp 做 HMAC-SHA256，取前 32 位十六进制（128 bit 足够抗暴力）。"""
    digest = hmac.new(secret.encode(), f"{raw}|{exp}".encode(), hashlib.sha256)
    return digest.hexdigest()[:32]


def split_urls(urls: str) -> list[str]:
    """空格 / 换行一律算分隔符；逗号只在「每段都像完整 URL」时才当分隔符。

    无脑 replace(",", " ") 会切碎带逗号的合法 URL（...?ids=1,2 → 第二段 "2"），
    结果是真链接被打成 error 条目，现象看起来像域名白名单误伤。
    """
    normalized = urls.replace(chr(10), " ").replace(chr(13), " ")
    out: list[str] = []
    for part in normalized.split():
        # 尾部逗号（"url1, url2" 这种写法）先摘掉，否则整段解析必然失败
        trimmed = part.rstrip(",")
        if not trimmed:
            continue
        if "," not in trimmed:
            out.append(trimmed)
            continue
        chunks = [c.strip() for c in trimmed.split(",") if c.strip()]
        if len(chunks) > 1 and all("://" in c for c in chunks):
            out.extend(chunks)
        else:
            out.append(trimmed)
    return out


def build_image_links(
    urls: str,
    public_base: str,
    secret: str,
    ttl_minutes: object = DEFAULT_TTL_MINUTES,
    now: float | None = None,
) -> dict:
    """生成签名外链；非法域名逐条带 error，不影响其它条目。

    public_base 为空直接返回 error：否则会产出根相对链接 /img?u=...，
    在聊天客户端里既打不开也看不出原因。重复 URL 去重保序——链接完全相同，
    重复只会让调用方以为拿到了两张不同的图。
    """
    ttl = clamp_ttl(ttl_minutes)
    base = (public_base or "").strip().rstrip("/")
    if not base:
        return {
            "count": 0,
            "ttl_minutes": ttl,
            "error": "缺少 public_base（对外访问前缀，如 https://mcp.example.com）",
            "items": [],
        }
    exp = int(time.time() if now is None else now) + ttl * 60
    items: list[dict] = []
    seen: set[str] = set()
    for raw in split_urls(urls):
        if raw in seen:
            continue
        seen.add(raw)
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https") or host not in IMAGE_HOSTS:
            items.append({"url": raw, "error": _HOST_ERROR})
            continue
        sig = sign_image(raw, secret, exp)
        items.append(
            {
                "url": raw,
                "link": f"{base}/img?u={quote(raw, safe='')}&exp={exp}&sig={sig}",
                "expires_at": exp,
            }
        )
    return {"count": len(items), "ttl_minutes": ttl, "items": items}
