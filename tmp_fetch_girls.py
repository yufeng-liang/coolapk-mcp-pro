# -*- coding: utf-8 -*-
"""从【我在酷安晒女友】话题取不同作者的图片，另存为候选。"""
import asyncio
import io
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

from coolapk_mcp.client import CoolapkClient
from coolapk_mcp.config import AppConfig

OUT = r"H:\coolapk-auto-post\tmp_promo_verify\imgs"


def pics(item):
    urls = []
    for k in ("picArr", "pic", "cover"):
        v = item.get(k)
        if isinstance(v, str) and v.startswith("http"):
            urls.append(v)
    mro = item.get("message_raw_output")
    if isinstance(mro, str):
        try:
            mro = json.loads(mro)
        except Exception:
            mro = None
    for b in (mro or []):
        if b.get("type") == "image":
            u = b.get("url") or ""
            if u.startswith("http"):
                urls.append(u)
    return urls


async def main():
    cfg = AppConfig.load()
    print("login:", cfg.is_logged_in, cfg.uid, cfg.username)
    c = CoolapkClient(cfg)
    try:
        feeds = await c.get_topic_feeds("我在酷安晒女友", page=1)
        print("feeds:", len(feeds))
        seen = set()
        n = 0
        for it in feeds:
            u = str(it.get("username") or it.get("userInfo", {}).get("username") or "")
            uid = it.get("uid") or it.get("userInfo", {}).get("uid")
            ps = pics(it)
            if not ps:
                continue
            key = (uid, u)
            if key in seen:
                continue
            seen.add(key)
            n += 1
            print(f"[{n}] uid={uid} user={u} pics={len(ps)} {ps[0][:80]}")
            for i, url in enumerate(ps[:3]):
                try:
                    raw, mime = await c.download_image(url)
                except Exception as e:
                    print("   dl fail", e)
                    continue
                name = f"g{n}_{i}.jpg"
                with open(os.path.join(OUT, name), "wb") as fh:
                    fh.write(raw)
                print("   saved", name, len(raw))
            if n >= 6:
                break
    finally:
        await c.close()


asyncio.run(main())
