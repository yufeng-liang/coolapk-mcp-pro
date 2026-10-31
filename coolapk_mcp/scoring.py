"""质量评分 — 帖子热度、讨论活跃度、作者权威度。

设计依据（2026-10 实测，12 个作者样本）：

- 粉丝数与等级强相关：11 级普遍 4–8 万粉，10 级约 4.7 万，9 级约 1.4 万，
  6 级以下多在 1 万内。用「粉丝 ≥ 1 万且等级 ≥ 8」能把真大佬干净筛出来。
- 帖子火 ≠ 作者权威：5 级 4.7 千粉的烟雨ovo 有 3102 赞单帖爆款，
  而 4 条动态的酒精过敏丶（56 粉）也有 759 赞。所以帖子分与作者分
  必须分开算，不能互相替代。
- 评论比点赞更能代表讨论热度：莫离然然解锁教程 6907 赞 / 11493 评，
  评论数超过点赞数，说明帖子在解决问题而不只是被围观。
"""

from __future__ import annotations

# 作者分级阈值（实测样本落点见模块 docstring）
TIER_TOP_FANS = 30000
TIER_TOP_LEVEL = 10
TIER_EXPERT_FANS = 10000
TIER_EXPERT_LEVEL = 8
TIER_ACTIVE_FANS = 1000
TIER_ACTIVE_LEVEL = 5

_TIER_LABEL = {
    "top": "领域顶流",
    "expert": "该领域大佬",
    "active": "活跃创作者",
    "normal": "普通用户",
}


def _as_int(value: object) -> int:
    """酷安接口的计数字段常见 str / int 混用，统一转成整数。"""
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            return 0


def post_score(like_num: object = 0, reply_num: object = 0, forward_num: object = 0) -> int:
    """帖子热度分：点赞 + 评论×3 + 转发×2。

    评论权重最高 —— 评论多说明帖子在解决具体问题、引发真实讨论，
    单纯点赞只能说明「被看到」。实测教程类帖子评论常超过点赞。
    """
    return _as_int(like_num) + _as_int(reply_num) * 3 + _as_int(forward_num) * 2


def discussion_ratio(like_num: object = 0, reply_num: object = 0) -> float:
    """评论/点赞比。> 1 表示讨论热度高于点赞，常见于教程、求助与争议帖。"""
    likes = _as_int(like_num)
    replies = _as_int(reply_num)
    if likes <= 0:
        return 0.0
    return round(replies / likes, 2)


def author_tier(level: object = 0, fans_num: object = 0, feed_num: object = 0) -> str:
    """作者权威等级：top / expert / active / normal。

    粉丝数主导，等级作辅助，产出量用于区分「精发大佬」与「大量水帖」：
    - top：粉丝 ≥ 3 万且等级 ≥ 10（实测莫离然然 7.9 万粉/11 级）
    - expert：粉丝 ≥ 1 万且等级 ≥ 8（实测 d2u8 1.36 万粉/9 级）
    - active：粉丝 ≥ 1000 且等级 ≥ 5
    - normal：其余
    """
    fans = _as_int(fans_num)
    lvl = _as_int(level)
    if fans >= TIER_TOP_FANS and lvl >= TIER_TOP_LEVEL:
        return "top"
    if fans >= TIER_EXPERT_FANS and lvl >= TIER_EXPERT_LEVEL:
        return "expert"
    if fans >= TIER_ACTIVE_FANS and lvl >= TIER_ACTIVE_LEVEL:
        return "active"
    return "normal"


def tier_label(tier: str) -> str:
    """作者等级的中文说明，写进返回里方便 agent 直接用。"""
    return _TIER_LABEL.get(tier, "普通用户")


# 专长领域关键词表：bio 与帖子文本命中即计分，取前几名
EXPERTISE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "解锁/Root": ("解锁", "bl", "bootloader", "root", "提权", "magisk", "面具"),
    "刷机/救砖": ("刷机", "rom", "固件", "线刷", "9008", "救砖", "刷入", "降级"),
    "模块/内核": ("模块", "lsposed", "kernelsu", "sukisu", "内核", "kernel", "框架"),
    "美化/主题": ("美化", "主题", "状态栏", "字体", "图标", "壁纸"),
    "省电/性能": ("省电", "续航", "性能", "调度", "温控", "功耗", "充电"),
}


def infer_expertise(bio: str = "", texts: object = ()) -> list[str]:
    """从作者 bio 与近期帖子文本推断专长领域（最多 3 个，按命中文档数排序）。

    texts 可以是字符串或字符串列表；只做关键词计数，不依赖外部模型，
    保证 MCP 进程零额外依赖、零启动延迟。
    """
    if isinstance(texts, str):
        corpus = [texts]
    elif isinstance(texts, (list, tuple)):
        corpus = [str(t)[:600] for t in texts if t]
    else:
        corpus = []
    blob = (str(bio or "") + "\n" + "\n".join(corpus)).lower()
    if not blob.strip():
        return []
    hits: list[tuple[int, str]] = []
    for field, words in EXPERTISE_KEYWORDS.items():
        count = sum(blob.count(w) for w in words)
        if count > 0:
            hits.append((count, field))
    hits.sort(key=lambda x: (-x[0], x[1]))
    return [field for _, field in hits[:3]]


def summarize_author(profile: dict) -> dict:
    """把作者资料补上质量派生字段，供 coolapk_author / author_digest 直接返回。

    profile 里的 bio、最近帖子文本（可选 posts_texts 键）都参与专长推断。
    """
    level = profile.get("level")
    fans = profile.get("fans_num")
    feeds = profile.get("feed_num")
    tier = author_tier(level, fans, feeds)
    fans_int = _as_int(fans)
    feeds_int = _as_int(feeds)
    texts = profile.get("posts_texts") or []
    return {
        "author_tier": tier,
        "author_tier_label": tier_label(tier),
        "is_expert": tier in ("top", "expert"),
        "fans_per_feed": round(fans_int / feeds_int) if feeds_int > 0 else 0,
        "expertise": infer_expertise(profile.get("bio", ""), texts),
    }


def annotate_post(item: dict) -> dict:
    """给帖子条目补 post_score / discussion_ratio，原地返回同一个 dict。"""
    item["post_score"] = post_score(
        item.get("like_num"), item.get("reply_num"), item.get("forward_num")
    )
    item["discussion_ratio"] = discussion_ratio(
        item.get("like_num"), item.get("reply_num")
    )
    return item
