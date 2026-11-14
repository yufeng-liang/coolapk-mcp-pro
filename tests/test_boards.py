"""boards.py 回归测试：板块 ID / page_name / 完整 URL 三种入参的解析。"""

from __future__ import annotations

from coolapk_mcp.boards import BOARD_URLS, resolve_board_url


def test_resolve_board_id_returns_mapped_url() -> None:
    """板块 ID 命中映射表，直接返回表里的 url 而非拼装。"""
    assert resolve_board_url("digital") == "/page?url=V10_DIGITAL_HOME"
    assert resolve_board_url("hot") == "/page?url=V9_HOME_TAB_RANKING"
    # 非 /page 形态的板块（头条走 /main、二手走 hash 路由）同样要原样返回
    assert resolve_board_url("headline") == "/main/headline"
    assert resolve_board_url("secondhand") == "#/feed/ershouList?dataListType=staggered"


def test_resolve_page_name_matches_board_url() -> None:
    """传 page_name（V10_DIGITAL_HOME）时反查映射表，结果与传板块 ID 完全一致。"""
    assert resolve_board_url("V10_DIGITAL_HOME") == resolve_board_url("digital")
    assert resolve_board_url("V9_HOME_TAB_RANKING") == "/page?url=V9_HOME_TAB_RANKING"


def test_resolve_full_url_passthrough() -> None:
    """已带 / 或 # 的完整 url 不再二次拼装。"""
    assert resolve_board_url("/page?url=V99_CUSTOM") == "/page?url=V99_CUSTOM"
    assert resolve_board_url("#/feed/ershouList?brand=1005") == "#/feed/ershouList?brand=1005"


def test_resolve_unknown_board_falls_back_to_page_url() -> None:
    """未收录的 page_name 兜底拼成 /page?url=<name>，不抛异常。"""
    assert resolve_board_url("V99_NOT_EXIST") == "/page?url=V99_NOT_EXIST"


def test_resolve_uses_injected_mapping() -> None:
    """board_urls 可注入：解析逻辑只依赖入参映射，便于上层替换板块表。"""
    custom = {"myboard": ("我的板块", "/page?url=V99_MINE")}
    assert resolve_board_url("myboard", custom) == "/page?url=V99_MINE"
    # 注入表里没有的 ID 不会被真实表命中，走 page_name 兜底
    assert resolve_board_url("digital", custom) == "/page?url=digital"
    assert BOARD_URLS["digital"][1] == "/page?url=V10_DIGITAL_HOME"


def test_board_mapping_entries_are_well_formed() -> None:
    """映射表每条都是 (标题, url) 二元组且 url 非空，防止手改表时漏字段。

    不锁板块总数——新增板块不应让本用例变红。
    """
    assert len(BOARD_URLS) >= 20
    for board_id, entry in BOARD_URLS.items():
        title, url = entry
        assert board_id and title and url
