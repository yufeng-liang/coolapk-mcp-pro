"""pytest 全局夹具

1) 把仓库根加入 sys.path：pytest 在 rootdir 下只把 tests/ 目录插入 sys.path
   （tests 无 __init__.py 时），直接 `import coolapk_mcp` 会失败。
2) isolated_config：把 config 模块的 CONFIG_DIR / CONFIG_FILE 指向 tmp_path，
   测试不会读写真实的 ~/.coolapk-mcp，结束后由 monkeypatch 自动还原。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    """隔离配置目录：CONFIG_DIR/CONFIG_FILE 指向 tmp_path，退出时恢复原值

    config.py 里 load()/save() 都读模块级全局常量（config.py:14-15、155、202-215），
    因此 patch 模块属性即可生效，无需改业务代码。
    """
    from coolapk_mcp import config

    config_dir = tmp_path / ".coolapk-mcp"
    config_dir.mkdir()
    config_file = config_dir / "config.json"

    monkeypatch.setattr(config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config, "CONFIG_FILE", config_file)
    return config_file


@pytest.fixture(autouse=True)
def isolated_uid_index(tmp_path, monkeypatch):
    """隔离 uid 索引文件：CoolapkClient.INDEX_FILE 指向 tmp_path

    _index_feeds 在列表响应后会写 ~/.coolapk-mcp/uid_index.json（本轮改成延迟 +
    线程卸载写盘，测试结束时可能仍有在途任务），不隔离会污染开发者本机真实索引。
    """
    from coolapk_mcp.client import CoolapkClient

    index_file = tmp_path / "uid_index.json"
    monkeypatch.setattr(CoolapkClient, "INDEX_FILE", index_file)
    return index_file
