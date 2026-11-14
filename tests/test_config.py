"""config.py 回归测试：配置往返、坏文件兜底、设备码补全与登录态判定。

一律走 conftest.isolated_config（CONFIG_DIR/CONFIG_FILE 指向 tmp_path）并锁定
COOLAPK_LOGIN_MODE=native，绝不触碰真实的 ~/.coolapk-mcp 与桌面端 accounts.json。
"""

from __future__ import annotations

import json

import pytest

from coolapk_mcp import config

ENV_MODE = "COOLAPK_LOGIN_MODE"


@pytest.fixture()
def native_mode(monkeypatch):
    """锁定 native 登录态来源，避免 auto 模式去读真实桌面端账号文件。"""
    monkeypatch.setenv(ENV_MODE, "native")
    return "native"


def test_save_then_load_roundtrip(isolated_config, native_mode) -> None:
    """save 落盘 JSON，load 读回同一份字段；完整 Cookie 串参与登录态解析。"""
    cfg = config.AppConfig()
    cfg.device_code = "DEV-CODE-1"
    cfg.uid = "123"
    cfg.username = "酷安用户"
    cfg.token = "tk"
    cfg.api_base = "https://api.example.com"
    cfg.desktop_cookie = "SESSID=valid-sessid; uid=123; token=tk"

    cfg.save()
    assert isolated_config.exists()

    raw = json.loads(isolated_config.read_text(encoding="utf-8"))
    assert raw["device_code"] == "DEV-CODE-1"
    assert raw["api_base"] == "https://api.example.com"
    # save 只写这几个键，uid/token 不能出现在临时文件里残留
    assert not isolated_config.with_suffix(".json.tmp").exists()

    loaded = config.AppConfig.load()
    assert loaded.device_code == "DEV-CODE-1"
    assert loaded.uid == "123"
    assert loaded.username == "酷安用户"
    assert loaded.token == "tk"
    assert loaded.api_base == "https://api.example.com"
    assert loaded.desktop_cookie == cfg.desktop_cookie
    assert loaded.is_logged_in is True
    assert loaded.source == config.SOURCE_NATIVE


def test_bad_json_falls_back_to_empty_config(isolated_config, native_mode) -> None:
    """配置文件被写坏时不抛异常，按空配置继续，并补上设备码重新落盘。"""
    isolated_config.write_text("{ this is not json", encoding="utf-8")

    loaded = config.AppConfig.load()

    assert loaded.uid == ""
    assert loaded.token == ""
    assert loaded.username == ""
    assert loaded.api_base == "https://api.coolapk.com"
    assert loaded.is_logged_in is False
    assert loaded.source == config.SOURCE_NONE
    # 兜底路径同样补设备码，并把坏文件覆盖成合法 JSON
    assert loaded.device_code
    assert json.loads(isolated_config.read_text(encoding="utf-8"))["device_code"] == (
        loaded.device_code
    )


def test_missing_device_code_is_generated_and_persisted(isolated_config, native_mode) -> None:
    """无 device_code 时生成一个并立即落盘；再次 load 拿到同一个值。"""
    isolated_config.write_text(json.dumps({"uid": "1", "token": "t"}), encoding="utf-8")

    first = config.AppConfig.load()
    assert first.device_code
    assert len(first.device_code) == 36  # uuid4 字符串长度

    assert json.loads(isolated_config.read_text(encoding="utf-8"))["device_code"] == (
        first.device_code
    )

    second = config.AppConfig.load()
    assert second.device_code == first.device_code
    assert second.uid == "1"
    assert second.token == "t"


def test_is_logged_in_requires_uid_and_token(isolated_config, native_mode) -> None:
    """uid 与 token 缺一不可；未登录时 get_cookies 返回空 dict。"""
    cfg = config.AppConfig()
    assert cfg.is_logged_in is False
    assert cfg.get_cookies() == {}

    cfg.uid = "123"
    assert cfg.is_logged_in is False

    cfg.token = "tk"
    assert cfg.is_logged_in is True
    assert cfg.get_cookies() == {"uid": "123", "username": "", "token": "tk"}

    cfg.uid = ""
    assert cfg.is_logged_in is False
    assert cfg.get_cookies() == {}


def test_login_mode_env_is_normalized(monkeypatch) -> None:
    """env 里写大小写/非法值都不崩：合法值小写生效，非法值回落 auto。"""
    monkeypatch.setenv(ENV_MODE, " NATIVE ")
    assert config.get_login_mode() == "native"

    monkeypatch.setenv(ENV_MODE, "bogus")
    assert config.get_login_mode() == "auto"

    monkeypatch.delenv(ENV_MODE)
    assert config.get_login_mode() == "auto"
