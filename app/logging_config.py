"""Structured JSON-Logging (Spec Kap. 6.3).

Jeder Log-Eintrag ist eine JSON-Zeile mit timestamp/level/message plus
beliebigen Zusatzfeldern (task_type, sale_id, duration_ms, ...), die ueber
`logger.info(msg, extra={...})` mitgegeben werden.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

# Standard-LogRecord-Attribute, die NICHT als Zusatzfeld ausgegeben werden.
_RESERVED = set(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Zusatzfelder aus `extra=...`
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn-Access-Logs nicht doppelt ausgeben
    logging.getLogger("uvicorn.access").handlers.clear()
