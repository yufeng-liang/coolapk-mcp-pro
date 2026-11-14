"""帖子标识解析：把分享链接 / 分享文案 / 纯数字统一成 feed_id。

为什么单独成模块：coolapk_feed_detail（server）与 coolapk feed（CLI）都要
接受「用户从客户端复制出来的一整段分享文本」，两处各写一遍正则必然漂移，
一处改了另一处没改就会出现「聊天里能看、命令行报错」的割裂。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# 酷安分享形态：
#   https://www.coolapk.com/feed/1234567?shareKey=...
#   https://www.coolapk.com/feed/detail?id=1234567
#   带参数的跳转：feedId=1234567 / fid=1234567
_LINK_PATTERNS = (
    re.compile(r"coolapk\.com/feed/(\d+)", re.IGNORECASE),
    re.compile(r"coolapk\.com/(?:m/)?feed/detail[^\s]*?[?&]id=(\d+)", re.IGNORECASE),
    re.compile(r"(?:feedId|feed_id|fid)=(\d+)", re.IGNORECASE),
)
# 兜底：整段文案里 6-9 位的数字段（酷安 feed id 早已是 7-9 位）。
# 用 6 位起而不是 4 位，是为了不把「第 3 张图」「2024 年」这类数字当成帖子 ID；
# 上限 9 位且要求两侧不再是数字，是为了不把手机号（11 位）、订单号这类长数字
# 当成帖子 ID——那会去请求一条不存在的帖子，报错指向完全无关的地方。
_DIGIT_RUN = re.compile(r"(?<!\d)\d{6,9}(?!\d)")
# 文案里的 http(s) 链接，用来判断这段文本是不是「从别的网站复制来的」
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
# 只有酷安自家的域名才认链接形态；其它站点即使路径长得像 /feed/123 也不算
_COOLAPK_HOSTS = ("coolapk.com", "coolapk.com.cn", "coolapk.net")


def _has_foreign_url(text: str) -> bool:
    """文案里是否含指向非酷安站点的 http(s) 链接。

    为什么要拦：数字兜底是给「酷安链接被客户端拆掉、只剩一串数字」的文案用的，
    而一段 GitHub issue 链接或任意文章链接里也有 6-9 位数字，猜下去就会去请求
    一条完全无关的帖子（实测 https://github.com/foo/bar/issues/123456 → 123456）。
    含外部链接时只认显式酷安链接，数字兜底一律放弃。
    """
    for raw in _URL_RE.findall(text):
        host = (urlsplit(raw).hostname or "").lower()
        if not host.endswith(_COOLAPK_HOSTS):
            return True
    return False


def parse_feed_id(value: object) -> int | None:
    """从 int / 纯数字串 / 分享链接 / 整段分享文案里取出 feed_id。

    取不到或有歧义时返回 None：文案里出现多个候选说明这条文本可能带了多条
    帖子，宁可让调用方补一个明确的 ID，也不要猜错帖子把别人的内容当成用户
    要的那条。因此链接形态也要先收集全部候选、去重后只剩一个才采用，不能
    「第一个正则命中就返回」——同一段文案里两条不同链接会被静默猜成第一条。
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
        # 纯数字是「用户明确给的 ID」：按原样采纳，不再套 6-9 位规则；
        # 手机号/订单号这类长数字只在整段文案里出现时才由下面的兜底拒绝
        number = int(text)
        return number if number > 0 else None
    linked = {int(m.group(1)) for pattern in _LINK_PATTERNS for m in pattern.finditer(text)}
    if linked:
        # 有酷安链接就只信链接（文案里同时出现手机号/图片序号时不该被它们干扰）
        return linked.pop() if len(linked) == 1 else None
    if _has_foreign_url(text):
        return None
    candidates = {int(run) for run in _DIGIT_RUN.findall(text)}
    if len(candidates) == 1:
        return candidates.pop()
    return None
