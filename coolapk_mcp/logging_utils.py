"""日志配置：默认不动根 logger，COOLAPK_LOG_JSON=1 时切成 JSON Lines。

stdio 传输把 stdout 让给协议帧，日志必须全走 stderr；容器里由采集器按行
解析 JSON 即可，不引入额外依赖（也就不用为结构化日志装 structlog）。
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

_TRUTHY = ("1", "true", "yes", "on")


def _enabled() -> bool:
    return os.environ.get("COOLAPK_LOG_JSON", "").strip().lower() in _TRUTHY


class JsonFormatter(logging.Formatter):
    """一条日志一行 JSON：ts/level/logger/msg，异常只保留最后一行。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            lines = self.formatException(record.exc_info).strip().splitlines()
            if lines:
                payload["exc"] = lines[-1]
        line = json.dumps(payload, ensure_ascii=False)
        # U+2028/U+2029/U+0085 不被 ensure_ascii=False 转义，却会被按行切分的
        # 采集器当成换行：转义掉才能保证「一条日志一行」
        return line.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029").replace("\u0085", "\\u0085")


def setup_logging(level: str | None = None) -> None:
    """按环境变量装配日志；未开启 JSON 时保持 Python 默认行为（不装 handler）。"""
    if not _enabled():
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler._coolapk_json = True  # type: ignore[attr-defined]
    root = logging.getLogger()
    # 只替换自己上次装的那个：宿主（pytest、外层 uvicorn）已有的 handler 不该被吞
    kept = [h for h in root.handlers if not getattr(h, "_coolapk_json", False)]
    root.handlers[:] = [*kept, handler]
    # httpx/mcp 的 INFO 会把完整请求 URL（可能带 token/sessid 查询串）写进日志流，
    # 统一压到 WARNING，只留真正的告警
    for noisy in ("httpx", "httpcore", "mcp", "uvicorn"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    raw_level = level or os.environ.get("COOLAPK_LOG_LEVEL") or "INFO"
    effective = raw_level.upper()
    try:
        root.setLevel(effective)
    except ValueError:
        # COOLAPK_LOG_LEVEL 写错（如 verbose）不该让每条 CLI 命令都崩在启动，
        # 回落 INFO 并把问题说清楚
        print(
            f"[coolapk-mcp] COOLAPK_LOG_LEVEL={raw_level!r} 不是有效日志级别，回落 INFO",
            file=sys.stderr,
        )
        root.setLevel(logging.INFO)
