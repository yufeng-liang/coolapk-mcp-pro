"""scoring.py 回归测试：帖子热度、讨论比与作者分级。

阈值是实测样本定下来的（见 scoring.py 模块 docstring），这里把它们钉成回归
用例：改阈值就必须同时改测试，避免静默改变搜索结果的排序与画像结论。
"""

from __future__ import annotations

import pytest

from coolapk_mcp.scoring import (
    EXPERTISE_KEYWORDS,
    author_tier,
    discussion_ratio,
    infer_expertise,
    post_score,
    summarize_author,
    tier_label,
)


def test_post_score_weights_reply_and_forward() -> None:
    """post_score = 点赞 + 评论×3 + 转发×2。"""
    assert post_score(10, 2, 3) == 10 + 2 * 3 + 3 * 2
    assert post_score(10, 2, 3) == 22
    # 评论权重高于转发高于点赞：同样 1 个单位的互动，分数必须严格递减
    assert post_score(like_num=1) == 1
    assert post_score(reply_num=1) == 3
    assert post_score(forward_num=1) == 2


def test_post_score_coerces_string_and_empty_values() -> None:
    """酷安接口计数常见 str/int 混用，空值与非法值一律当 0。"""
    assert post_score("10", "2", "3") == 22
    assert post_score(None, None, None) == 0
    assert post_score("", "", "") == 0
    assert post_score("not-a-number", 1, 1) == 5


def test_discussion_ratio_boundaries() -> None:
    """点赞为 0/负数时返回 0.0（不做除零），正常值保留两位小数。"""
    assert discussion_ratio(0, 100) == 0.0
    assert discussion_ratio(0, 0) == 0.0
    assert discussion_ratio(like_num=0) == 0.0
    assert discussion_ratio(10, 5) == 0.5
    assert discussion_ratio(10, 30) == 3.0
    # 10/3 = 3.333... 取两位
    assert discussion_ratio(3, 10) == pytest.approx(3.33)
    # 教程帖：评论数超过点赞数，比值 > 1
    assert discussion_ratio(6907, 11493) > 1


def test_author_tier_thresholds() -> None:
    """三档阈值都是「且」关系：粉丝数与等级同时达标才升级。"""
    # top：粉丝 ≥ 30000 且等级 ≥ 10
    assert author_tier(10, 30000) == "top"
    assert author_tier(11, 79000) == "top"
    # 差一个条件就退到下一档
    assert author_tier(10, 29999) == "expert"
    assert author_tier(9, 30000) == "expert"
    # expert：粉丝 ≥ 10000 且等级 ≥ 8
    assert author_tier(8, 10000) == "expert"
    assert author_tier(7, 10000) == "active"
    # active：粉丝 ≥ 1000 且等级 ≥ 5
    assert author_tier(5, 1000) == "active"
    assert author_tier(4, 1000) == "normal"
    assert author_tier(5, 999) == "normal"
    # 缺省值 / 字符串计数
    assert author_tier() == "normal"
    assert author_tier("10", "30000") == "top"


def test_tier_label_mapping() -> None:
    """中文标签写进返回体，未知档位兜底「普通用户」。"""
    assert tier_label("top") == "领域顶流"
    assert tier_label("expert") == "该领域大佬"
    assert tier_label("active") == "活跃创作者"
    assert tier_label("normal") == "普通用户"
    assert tier_label("unknown-tier") == "普通用户"


def test_infer_expertise_hits_and_misses() -> None:
    """bio 与帖子文本都参与关键词计数；无命中返回空列表而不是空字符串。"""
    hit = infer_expertise("解锁 bootloader，Magisk 面具玩家")
    assert hit[0] == "解锁/Root"
    assert all(field in EXPERTISE_KEYWORDS for field in hit)

    # texts 传字符串与传列表等价
    assert infer_expertise("", "刷机 线刷 救砖")[0] == "刷机/救砖"
    assert infer_expertise("", ["刷机 线刷 救砖"])[0] == "刷机/救砖"

    # 无命中：返回空列表
    assert infer_expertise("今天天气不错", ["随便看看"]) == []
    assert infer_expertise("", []) == []
    assert infer_expertise("", [""]) == []

    # 最多返回 3 个领域，按命中次数降序
    many = infer_expertise("解锁 root 刷机 模块 美化 省电")
    assert len(many) == 3
    assert many[0] == "解锁/Root"


def test_summarize_author_output_keys_and_values() -> None:
    """派生字段齐全，且 fans_per_feed / expertise 由入参真实算出。"""
    profile = {
        "uid": 9,
        "username": "大佬",
        "level": 11,
        "fans_num": 79000,
        "feed_num": 100,
        "bio": "解锁 bootloader，Magisk 面具玩家",
        "posts_texts": ["刷机 线刷 救砖"],
    }

    out = summarize_author(profile)

    assert set(out) == {
        "author_tier",
        "author_tier_label",
        "is_expert",
        "fans_per_feed",
        "expertise",
    }
    assert out["author_tier"] == "top"
    assert out["author_tier_label"] == "领域顶流"
    assert out["is_expert"] is True
    assert out["fans_per_feed"] == 790
    assert out["expertise"][0] == "解锁/Root"

    # 未发帖（feed_num=0）不做除零，普通用户不是专家
    quiet = summarize_author({"level": 3, "fans_num": 50, "feed_num": 0})
    assert quiet["author_tier"] == "normal"
    assert quiet["is_expert"] is False
    assert quiet["fans_per_feed"] == 0
    assert quiet["expertise"] == []
