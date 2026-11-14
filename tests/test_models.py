"""models.py 回归测试：dateline 东八区格式化与评论链接提取。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from coolapk_mcp.models import (
    ChatSessionModel,
    FeedModel,
    MessageModel,
    ReplyModel,
    UserModel,
    _extract_links,
    _format_time,
)


# ---------------------------------------------------------------------------
# dateline：酷安是东八区 Unix 秒，输出必须带 +08:00 偏移
# ---------------------------------------------------------------------------


def test_dateline_1700000000_is_east8_0600() -> None:
    """1700000000 = UTC 2023-11-14 22:13:20，东八区应为 2023-11-15 06:13。"""
    feed = FeedModel(dateline=1700000000)
    assert feed.dateline.endswith("+08:00")
    assert feed.dateline == "2023-11-15T06:13+08:00"

    parsed = datetime.fromisoformat(feed.dateline)
    assert parsed.hour == 6
    assert parsed.utcoffset() == timedelta(hours=8)

    # 接口偶尔把 dateline 返回成数字字符串，共用同一个格式化函数
    reply = ReplyModel(dateline="1700000000")
    assert reply.dateline == "2023-11-15T06:13+08:00"


def test_all_dateline_validators_share_east8_format() -> None:
    """私信/会话/注册时间等模型的 dateline 校验器共用 _format_time。"""
    assert MessageModel(dateline=1700000000).dateline == "2023-11-15T06:13+08:00"
    assert ChatSessionModel(dateline=1700000000).dateline == "2023-11-15T06:13+08:00"
    assert UserModel(regdate=1700000000).reg_date == "2023-11-15T06:13+08:00"


@pytest.mark.parametrize("value", [None, "", 0, "0"])
def test_empty_dateline_returns_empty_string(value: object) -> None:
    assert FeedModel(dateline=value).dateline == ""


@pytest.mark.parametrize("value", ["abc", "2023-11-15"])
def test_non_numeric_dateline_passthrough(value: str) -> None:
    """非数字字符串沿用原有回退：原样保留，不抛异常。"""
    assert FeedModel(dateline=value).dateline == value


@pytest.mark.parametrize("value", [10**20, float("nan"), float("inf")])
def test_unrepresentable_dateline_returns_empty(value: float) -> None:
    assert FeedModel(dateline=value).dateline == ""
    assert _format_time(value) == ""


def test_format_time_tolerates_junk_input() -> None:
    assert _format_time("abc") == ""
    assert _format_time(None) == ""  # type: ignore[arg-type]
    assert _format_time(0) == ""


# ---------------------------------------------------------------------------
# 链接提取：HTML 实体先还原，再去尾部标点，去重且保序
# ---------------------------------------------------------------------------


def test_extract_links_unescapes_html_entities() -> None:
    links = _extract_links('<a href="https://x/a?pwd=1&amp;y=2">下载</a>')
    assert links == ["https://x/a?pwd=1&y=2"]
    assert "&" in links[0]
    assert "&amp;" not in links[0]

    # 生产路径：ReplyModel 从原始 HTML message 提取链接
    reply = ReplyModel(message='<a href="https://x/a?pwd=1&amp;y=2">下载</a>')
    assert reply.links == ["https://x/a?pwd=1&y=2"]


def test_extract_links_dedupes_after_unescape_and_keeps_order() -> None:
    """同一链接分别以 &amp; 与 & 出现时，去重应发生在反转义之后。"""
    text = (
        "仓库 https://gitee.com/u/r "
        "网盘 https://x/a?pwd=1&amp;y=2 "
        "再来一次 https://x/a?pwd=1&y=2"
    )
    assert _extract_links(text) == [
        "https://gitee.com/u/r",
        "https://x/a?pwd=1&y=2",
    ]


def test_extract_links_strips_trailing_punctuation() -> None:
    # 现有 rstrip 集合：半角 ")" "." "," 与全角 "，" "。"
    assert _extract_links("见 (https://gitee.com/u/r.git).") == [
        "https://gitee.com/u/r.git"
    ]
    assert _extract_links("见 https://gitee.com/u/r.git。") == [
        "https://gitee.com/u/r.git"
    ]
    assert _extract_links("") == []
