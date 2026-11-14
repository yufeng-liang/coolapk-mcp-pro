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
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str | None = None) -> None:
    """按环境变量装配日志；未开启 JSON 时保持 Python 默认行为（不装 handler）。"""
    if not _enabled():
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    effective = level or os.environ.get("COOLAPK_LOG_LEVEL") or "INFO"
    root.setLevel(effective.upper())
