"""postfilter.filter_search_results 单元测试

覆盖：广告词命中、同 id 去重、同 uid+正文前 80 字去重、card 剔除、
strict 的 AND 语义、非 strict 下只命中部分关键词仍保留。
只用 dict 造数据，不触网、不依赖 models/scoring。
"""

from __future__ import annotations

from coolapk_mcp import postfilter


def _feed(
    item_id: int | str = 1,
    uid: int = 100,
    title: str = "",
    message: str = "",
    entity_type: str = "feed",
    **extra,
) -> dict:
    """构造一条酷安搜索结果原始条目（字段名与真实接口一致）"""
    item = {
        "entityType": entity_type,
        "id": item_id,
        "uid": uid,
        "messageTitle": title,
        "message": message,
        "userInfo": {"uid": uid, "username": "tester"},
    }
    item.update(extra)
    return item


def test_ad_pattern_marks_item_but_keeps_it():
    """命中广告特征词只打标记不删除，且不改动传入的原对象"""
    raw = _feed(item_id=1, title="小米15 秒解BL", message="<p>远程刷机服务，私信报价</p>")
    kept, meta = postfilter.filter_search_results([raw], keyword="")

    assert len(kept) == 1
    assert kept[0]["_suspected_ad"] is True
    assert meta["suspected_ads"] == 1
    # 原对象不能被打上标记：缓存里是同一引用，回写会污染缓存并导致二次计数
    assert "_suspected_ad" not in raw


def test_ad_pattern_case_and_space_insensitive():
    """归一化后匹配：大小写与空格差异不影响广告识别（'秒解 BL' 命中 '秒解bl'）"""
    raw = _feed(item_id=2, message="支持秒解 BL 锁")
    kept, meta = postfilter.filter_search_results([raw], keyword="", strict=False)

    assert meta["suspected_ads"] == 1
    assert kept[0]["_suspected_ad"] is True


def test_duplicate_id_removed():
    """同 id 去重：字符串数字 id 与整型 id 视为同一个"""
    items = [
        _feed(item_id=42, message="第一条正文"),
        _feed(item_id="42", message="第二条正文"),
    ]
    kept, meta = postfilter.filter_search_results(items, keyword="", strict=False)

    assert len(kept) == 1
    assert kept[0]["message"] == "第一条正文"
    assert meta["duplicates_removed"] == 1


def test_duplicate_same_uid_and_content_prefix80_removed():
    """同作者 + 正文前 80 字相同即去重（后 80 字不同也算重复）"""
    head = "酷安 15 周年活动规则说明" * 8  # 前 80 字完全一致
    items = [
        _feed(item_id=11, uid=777, message=head + "版本A"),
        _feed(item_id=12, uid=777, message=head + "版本B"),
    ]
    kept, meta = postfilter.filter_search_results(items, keyword="", strict=False)

    assert len(kept) == 1
    assert meta["duplicates_removed"] == 1


def test_same_content_different_uid_kept():
    """正文相同但作者不同不算重复（不同人各自发帖）"""
    message = "同样的转发内容" * 10
    items = [
        _feed(item_id=21, uid=1, message=message),
        _feed(item_id=22, uid=2, message=message),
    ]
    kept, meta = postfilter.filter_search_results(items, keyword="", strict=False)

    assert len(kept) == 2
    assert meta["duplicates_removed"] == 0


def test_card_entity_removed():
    """entityType=card 的赞助卡/话题卡被剔除，正常实体保留"""
    items = [
        _feed(item_id=1, entity_type="card", message="赞助商卡片"),
        _feed(item_id=2, message="正常帖子"),
    ]
    kept, meta = postfilter.filter_search_results(items, keyword="", strict=False)

    assert [it["id"] for it in kept] == [2]
    assert meta["cards_removed"] == 1


def test_strict_is_and_each_term_must_match():
    """strict 为 AND 语义：只命中一个关键词的条目被滤掉"""
    items = [
        _feed(item_id=1, title="小米 15 使用体验", message="屏幕和续航都很满意"),
        _feed(item_id=2, title="小米 14 使用体验", message="只提到一个词"),
    ]
    kept, meta = postfilter.filter_search_results(items, keyword="小米 15", strict=True)

    assert [it["id"] for it in kept] == [1]
    assert meta["keyword_filtered"] == 1


def test_strict_normalizes_keyword_and_text():
    """归一化后匹配：'#小米15Ultra#' 与关键词 '小米 15 ultra' 视为包含"""
    items = [_feed(item_id=1, title="#小米15Ultra# 开箱")]
    kept, meta = postfilter.filter_search_results(
        items, keyword="小米 15 ULTRA", strict=True
    )

    assert len(kept) == 1
    assert meta["keyword_filtered"] == 0


def test_non_strict_keeps_partial_keyword_match():
    """非 strict 不做全部命中要求：只命中一个关键词的条目仍然保留（OR 语义）"""
    items = [
        _feed(item_id=1, title="小米 15 使用体验"),
        _feed(item_id=2, title="只聊小米的杂谈"),
    ]
    kept, meta = postfilter.filter_search_results(items, keyword="小米 15", strict=False)

    assert [it["id"] for it in kept] == [1, 2]
    assert meta["keyword_filtered"] == 0


def test_strict_filters_only_feed_entities():
    """严格过滤只作用于 feed；user/topic/app 条目不会被关键词误杀"""
    items = [
        _feed(item_id=1, entity_type="user", title="张三"),
        _feed(item_id=2, title="完全不相关的帖子"),
    ]
    kept, meta = postfilter.filter_search_results(items, keyword="小米", strict=True)

    assert [it["id"] for it in kept] == [1]
    assert meta["keyword_filtered"] == 1


def test_non_list_input_returns_empty_meta():
    """接口异常返回时（list 以外）安全返回空结果并保留 raw_count=0"""
    kept, meta = postfilter.filter_search_results({"data": []}, keyword="小米")  # type: ignore[arg-type]

    assert kept == []
    assert meta["raw_count"] == 0
