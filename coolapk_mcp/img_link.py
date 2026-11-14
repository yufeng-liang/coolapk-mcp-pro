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
    """把 ttl 夹到 [1, 10080] 分钟；非法值回落默认 120。"""
    try:
        ttl = int(float(str(minutes)))
    except (TypeError, ValueError):
        return DEFAULT_TTL_MINUTES
    return max(1, min(ttl, MAX_TTL_MINUTES))


def sign_image(raw: str, secret: str, exp: int) -> str:
    """对 url|exp 做 HMAC-SHA256，取前 32 位十六进制（128 bit 足够抗暴力）。"""
    digest = hmac.new(secret.encode(), f"{raw}|{exp}".encode(), hashlib.sha256)
    return digest.hexdigest()[:32]


def split_urls(urls: str) -> list[str]:
    """逗号 / 空格 / 换行都算分隔符。"""
    normalized = urls.replace(chr(10), " ").replace(",", " ")
    return [part for part in normalized.split() if part]


def build_image_links(
    urls: str,
    public_base: str,
    secret: str,
    ttl_minutes: object = DEFAULT_TTL_MINUTES,
    now: float | None = None,
) -> dict:
    """生成签名外链；非法域名逐条带 error，不影响其它条目。"""
    ttl = clamp_ttl(ttl_minutes)
    exp = int(time.time() if now is None else now) + ttl * 60
    items: list[dict] = []
    for raw in split_urls(urls):
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https") or host not in IMAGE_HOSTS:
            items.append({"url": raw, "error": _HOST_ERROR})
            continue
        sig = sign_image(raw, secret, exp)
        items.append(
            {
                "url": raw,
                "link": f"{public_base}/img?u={quote(raw, safe='')}&exp={exp}&sig={sig}",
                "expires_at": exp,
            }
        )
    return {"count": len(items), "ttl_minutes": ttl, "items": items}
