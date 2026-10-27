"""JSON-логи в stdout. Текст документов в логи не пишется (см. spec, раздел 9)."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Всё, что передано через extra=..., попадает в лог отдельными полями.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO", sentry_dsn: str = "", environment: str = "dev") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    if sentry_dsn:
        try:
            import sentry_sdk
        except ImportError:
            logging.getLogger(__name__).warning("SENTRY_DSN set but sentry-sdk is not installed")
        else:
            sentry_sdk.init(dsn=sentry_dsn, environment=environment, send_default_pii=False)
