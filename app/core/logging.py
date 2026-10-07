"""Configuración de logging con redacción de secretos."""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.core.redaction import RedactionFilter, register_secret_values

if TYPE_CHECKING:
    from app.core.config import Settings

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def set_request_id(value: str | None) -> None:
    request_id_var.set(value)


def get_request_id() -> str | None:
    return request_id_var.get()


class JsonFormatter(logging.Formatter):
    """Formato JSON de una línea por registro, apto para agregadores."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        request_id = get_request_id()
        if request_id:
            payload["request_id"] = request_id
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class PlainFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        request_id = get_request_id()
        prefix = f"[{request_id}] " if request_id else ""
        marca_tiempo = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        base = f"{marca_tiempo} {record.levelname:<8} {prefix}{record.name}: {record.getMessage()}"
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


def configure_logging(settings: Settings) -> None:
    """Configura el logging raíz con el filtro de redacción aplicado a los handlers."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if settings.log_json else PlainFormatter())

    secrets: list[object] = [settings.jwt_secret_value]
    if settings.database_url:
        secrets.append(settings.database_url)
    register_secret_values(*secrets)
    handler.addFilter(RedactionFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    # Librerías ruidosas: se bajan de nivel y nunca se loguean sus tramas completas.
    for noisy, level in (
        ("httpx", logging.WARNING),
        ("httpcore", logging.WARNING),
        ("zeep", logging.WARNING),
        ("asyncio", logging.WARNING),
    ):
        logging.getLogger(noisy).setLevel(level)

    logging.getLogger(__name__).debug("Logging configurado en nivel %s", settings.log_level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
