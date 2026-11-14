"""帖子标识解析：把分享链接 / 分享文案 / 纯数字统一成 feed_id。

为什么单独成模块：coolapk_feed_detail（server）与 coolapk feed（CLI）都要
接受「用户从客户端复制出来的一整段分享文本」，两处各写一遍正则必然漂移，
一处改了另一处没改就会出现「聊天里能看、命令行报错」的割裂。
"""

from __future__ import annotations

import re

# 酷安分享形态：
#   https://www.coolapk.com/feed/1234567?shareKey=...
#   https://www.coolapk.com/feed/detail?id=1234567
#   带参数的跳转：feedId=1234567 / fid=1234567
_LINK_PATTERNS = (
    re.compile(r"coolapk\.com/feed/(\d+)", re.IGNORECASE),
    re.compile(r"coolapk\.com/(?:m/)?feed/detail[^\s]*?[?&]id=(\d+)", re.IGNORECASE),
    re.compile(r"(?:feedId|feed_id|fid)=(\d+)", re.IGNORECASE),
)
# 兜底：整段文案里 6 位以上的数字段（酷安 feed id 早已是 7-9 位）。
# 用 6 位起而不是 4 位，是为了不把「第 3 张图」「2024 年」这类数字当成帖子 ID。
_DIGIT_RUN = re.compile(r"\d{6,}")


def parse_feed_id(value: object) -> int | None:
    """从 int / 纯数字串 / 分享链接 / 整段分享文案里取出 feed_id。

    取不到或有歧义时返回 None：文案里出现多个候选数字说明这条文本可能带了
    多条帖子，宁可让调用方补一个明确的 ID，也不要猜错帖子把别人的内容当成
    用户要的那条。
    """
    if isinstance(value, bool):
        # bool 是 int 的子类，True 会被当成 1——明确拒绝
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        number = int(text)
        return number if number > 0 else None
    for pattern in _LINK_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    candidates = {int(run) for run in _DIGIT_RUN.findall(text)}
    if len(candidates) == 1:
        return candidates.pop()
    return None
