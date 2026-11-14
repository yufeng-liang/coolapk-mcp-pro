"""server.py / config.py 关键回归测试（全部离线，不发起任何网络请求）。"""

import asyncio
import json
import os

import pytest

from coolapk_mcp import config as config_module
from coolapk_mcp import server
from coolapk_mcp.budget import Budget, BudgetExceeded, FetchResult
from coolapk_mcp.client import CoolapkError


def test_catch_api_error_wraps_unexpected_exception() -> None:
    """未预期异常（ValueError）必须转成 internal 结构化 JSON，且不向上抛。"""

    @server._catch_api_error
    async def boom() -> str:
        raise ValueError("内部炸了")

    raw = asyncio.run(boom())  # 若异常逃逸，这里会直接抛，测试失败

    assert isinstance(raw, str)
    payload = json.loads(raw)
    assert payload["error_type"] == "internal"
    assert payload["error"]
    # 堆栈只进服务端日志，不能出现在返回给客户端的串里
    assert "Traceback" not in raw
    assert "内部炸了" not in raw


class _FakeClient:
    """只记录 aclose 是否被 await 到的假 client。"""

    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        # 故意让出一次事件循环：证明调用方真的 await 了，而不是只创建协程
        await asyncio.sleep(0)
        self.closed = True


def test_reset_client_awaits_aclose(monkeypatch: pytest.MonkeyPatch) -> None:
    """_reset_client 要真正 await 旧实例的 aclose()，并把全局引用清空。"""
    fake = _FakeClient()
    monkeypatch.setattr(server, "_client", fake)
    monkeypatch.setattr(server, "_client_fp", ("uid", "token", "cookie", "https://api.coolapk.com"))
    # 每个用例独立事件循环，避免复用别处创建的 Lock
    monkeypatch.setattr(server, "_client_lock", None)

    asyncio.run(server._reset_client())

    assert fake.closed is True, "aclose() 没有被 await 完成"
    assert server._client is None
    assert server._client_fp is None


def test_thumb_variant_keeps_query_string() -> None:
    """缩略 URL 只改 path：带 query 的 URL 必须原样保留查询串。"""
    src = (
        "https://image.coolapk.com/feed/2024/0101/abc@1080x1080.jpg"
        "?x-oss-process=image/resize,w_200&auth_key=1"
    )
    out = server._thumb_variant(src)

    assert out == (
        "https://image.coolapk.com/feed/2024/0101/abc@1080x1080.jpg.m.jpg"
        "?x-oss-process=image/resize,w_200&auth_key=1"
    )
    assert out is not None
    assert out.endswith("?x-oss-process=image/resize,w_200&auth_key=1")
    # 后缀必须落在 path 里，不能出现在 query 之后
    assert ".m.jpg?" in out
    # 无尺寸后缀的图不缩略；非 jpg 也不缩略
    assert server._thumb_variant("https://image.coolapk.com/a.jpg?x=1") is None
    assert server._thumb_variant("https://image.coolapk.com/a@1080x1080.png") is None


@pytest.mark.skipif(os.name != "posix", reason="600 权限位只在 POSIX 有意义（Windows 跳过）")
def test_config_save_sets_600(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """config.save 原子替换后，POSIX 下配置文件权限必须是 0600。"""
    target = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_module, "CONFIG_FILE", target)

    cfg = config_module.AppConfig()
    cfg.uid = "12345"
    cfg.token = "tok"
    cfg.save()

    assert target.exists()
    assert (target.stat().st_mode & 0o777) == 0o600
    assert json.loads(target.read_text(encoding="utf-8"))["uid"] == "12345"

# ---- 分页/预算接入：comments_focus 翻页、expand 预算、retry_after 透出 ----


def _feed_data(replynum: int = 7) -> dict:
    """最小可用的详情接口返回：够 FeedDetail.from_api 组装出标题/正文/评论数。"""
    return {
        "id": 1001,
        "message": "<p>工具帖正文</p>",
        "userInfo": {"username": "楼主", "uid": 9},
        "replynum": replynum,
    }


def _reply(rid: int, replynum: int = 0) -> dict:
    return {
        "id": rid,
        "message": f"<p>评论{rid}</p>",
        "userInfo": {"username": f"用户{rid}", "uid": rid},
        "replynum": replynum,
    }


def _patch_client(monkeypatch: pytest.MonkeyPatch, fake: object) -> None:
    """把 _get_client 换成返回假 client 的协程，全程不联网。"""

    async def _fake_get_client():
        return fake

    monkeypatch.setattr(server, "_get_client", _fake_get_client)


class _FocusClient:
    """comments_focus 用例：paged 方法照真实实现过预算闸门并返回 FetchResult。"""

    def __init__(self, author, hot) -> None:
        self._author = author
        self._hot = hot
        self.budgets: list[Budget] = []

    async def get_feed_detail_smart(self, feed_id, **kwargs) -> dict:
        return {"source": "api", "data": _feed_data(), "replies": []}

    async def _paged(self, res, budget) -> FetchResult:
        assert budget is not None, "server 必须把共享 budget 传给分页方法"
        self.budgets.append(budget)
        for _ in range(res.requests):  # 模拟每页一次上游请求
            budget.take()
        return res

    async def get_author_replies_paged(self, feed_id, max_pages=5, budget=None):
        assert max_pages == server.FOCUS_MAX_PAGES
        return await self._paged(self._author, budget)

    async def get_popular_replies_paged(self, feed_id, max_pages=5, budget=None):
        assert max_pages == server.FOCUS_MAX_PAGES
        return await self._paged(self._hot, budget)


def test_feed_detail_focus_meta_reports_paging(monkeypatch: pytest.MonkeyPatch) -> None:
    """smart 聚焦：两来源共用预算，focus_meta 要如实上报页数/截断/续抓页码。"""
    author = FetchResult(
        [_reply(11), _reply(12)], truncated=True, next_page=3, requests=2
    )
    hot = FetchResult([_reply(12), _reply(13)], truncated=False, requests=2)
    fake = _FocusClient(author=author, hot=hot)
    _patch_client(monkeypatch, fake)

    payload = json.loads(asyncio.run(server.coolapk_feed_detail(1001, comments_focus="smart")))
    meta = payload["comments_focus"]

    assert meta["mode"] == "smart"
    assert (meta["author_replies"], meta["hot_replies"]) == (2, 2)
    assert meta["total_shown"] == 3, "两个来源的 12 号评论要按 id 去重"
    assert meta["pages_fetched"] == 4, "两个来源各 2 页"
    assert meta["truncated"] is True
    assert meta["next_page"] == 3
    assert meta["budget"]["requests"] == 4
    assert fake.budgets[0] is fake.budgets[1], "两个来源必须共用同一份 budget"
    assert [r["id"] for r in payload["replies"]] == [11, 12, 13]


class _ExpandClient:
    """expand 用例：第 1 次返回截断结果，第 2 次直接预算耗尽。"""

    def __init__(self) -> None:
        self.calls = 0

    async def get_feed_detail_smart(self, feed_id, **kwargs) -> dict:
        # 两条父评论都还有子回复没内联（replynum=3 且 replyRows 为空）
        return {
            "source": "api",
            "data": _feed_data(),
            "replies": [_reply(21, 3), _reply(22, 3)],
        }

    async def get_sub_replies_paged(self, reply_id, page=1, max_pages=5, budget=None):
        assert budget is not None
        self.calls += 1
        budget.take()
        if self.calls == 1:
            return FetchResult([_reply(210)], truncated=True, next_page=2, requests=1)
        raise BudgetExceeded("预算耗尽")


def test_expand_sub_replies_keeps_partial_and_flags_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预算耗尽要停在已展开部分：保留第 1 条的子回复，并标记 sub_replies_truncated。"""
    fake = _ExpandClient()
    _patch_client(monkeypatch, fake)

    payload = json.loads(
        asyncio.run(server.coolapk_feed_detail(1001, expand_sub_replies=True))
    )

    assert fake.calls == 2, "第 2 次抛 BudgetExceeded 后应 break，不再发起第 3 次"
    assert payload["sub_replies_expanded"] == 1
    assert payload["sub_replies_truncated"] is True
    assert [r["id"] for r in payload["replies"][0]["sub_replies"]] == [210]
    assert "sub_replies" not in payload["replies"][1], "第 2 条父评论保持原样"


class _TruncatedRepliesClient:
    """all_replies / comment_keyword 用例：评论翻页撞上限，需透出截断状态。"""

    async def get_feed_detail_smart(self, feed_id, **kwargs) -> dict:
        return {
            "source": "api",
            "data": _feed_data(),
            "replies": FetchResult(
                [_reply(41)], truncated=True, next_page=31, requests=30
            ),
        }


def test_feed_detail_surfaces_all_replies_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """all_replies 撞页数上限时，顶层必须明确告诉 agent 还有评论没取。"""
    _patch_client(monkeypatch, _TruncatedRepliesClient())

    payload = json.loads(
        asyncio.run(server.coolapk_feed_detail(1001, all_replies=True))
    )

    assert payload["replies_truncated"] is True
    assert payload["replies_next_page"] == 31


def test_comment_keyword_reports_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关键词检索基于被截断的评论集时，comment_search 也要标出未扫完。"""
    _patch_client(monkeypatch, _TruncatedRepliesClient())

    payload = json.loads(
        asyncio.run(server.coolapk_feed_detail(1001, comment_keyword="评论"))
    )

    assert payload["comment_search"]["truncated"] is True
    assert payload["comment_search"]["next_page"] == 31


class _HistoryClient:
    """修改历史用例：lastupdate 是秒级时间戳，输出必须与全站时间格式一致。"""

    async def get_feed_detail_smart(self, feed_id, **kwargs) -> dict:
        return {"source": "api", "data": _feed_data(), "replies": []}

    async def get_feed_history(self, feed_id) -> list[dict]:
        return [{"id": 1, "message": "<p>第一版</p>", "lastupdate": 1700000000}]


def test_edit_history_dateline_is_iso_cst(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_client(monkeypatch, _HistoryClient())

    payload = json.loads(
        asyncio.run(server.coolapk_feed_detail(1001, with_history=True))
    )

    assert payload["edit_history"][0]["dateline"] == "2023-11-15T06:13+08:00"


class _DigestClient:
    """author_digest 用例：动态列表撞页数上限，需把截断信息透到包装结构。"""

    async def get_user_feeds_pages(
        self, uid, pages=2, start_page=1, feed_type="feed", budget=None
    ):
        assert budget is not None
        budget.take()
        return FetchResult(
            [{"id": 31, "message": "<p>帖子A</p>", "userInfo": {"username": "作者", "uid": uid}}],
            truncated=True,
            next_page=3,
            requests=1,
        )

    async def get_user_space(self, uid) -> dict:
        return {"uid": uid, "username": "作者"}


def test_author_digest_surfaces_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    """author_digest 的白名单必须放行 truncated/next_page，否则 agent 看不到还没取完。"""
    _patch_client(monkeypatch, _DigestClient())

    payload = json.loads(asyncio.run(server.coolapk_author_digest(7)))

    assert payload["truncated"] is True
    assert payload["next_page"] == 3
    assert [p["id"] for p in payload["posts"]] == [31]


def test_catch_api_error_passes_retry_after() -> None:
    """captcha 冷却错误要把 retry_after（秒，一位小数）透给调用方。"""

    @server._catch_api_error
    async def cooling() -> str:
        raise CoolapkError("触发风控", 1004, kind="captcha", retry_after=12.345)

    payload = json.loads(asyncio.run(cooling()))

    assert payload["error_type"] == "captcha"
    assert payload["code"] == 1004
    assert payload["retry_after"] == 12.3
    assert set(payload) == {"error", "error_type", "code", "retry_after"}


def test_catch_api_error_omits_retry_after_when_unknown() -> None:
    """无冷却信息的普通接口错误不应凭空多出 retry_after 键。"""

    @server._catch_api_error
    async def plain_fail() -> str:
        raise CoolapkError("接口报错", 500)

    payload = json.loads(asyncio.run(plain_fail()))

    assert payload["error_type"] == "api"
    assert "retry_after" not in payload
