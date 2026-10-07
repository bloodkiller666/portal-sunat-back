"""Middlewares transversales: identificador de petición y log de acceso."""

from __future__ import annotations

import time
import uuid

from fastapi import FastAPI, Request
from starlette.middleware.cors import CORSMiddleware

from app.core.config import Settings
from app.core.logging import get_logger, set_request_id

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-Id"


def register_cors(app: FastAPI, settings: Settings) -> None:
    """CORS configurable por variables de entorno (regla F)."""
    if not settings.cors_origins:
        logger.warning("CORS_ORIGINS está vacío: no se habilitó CORS")
        return

    allow_credentials = settings.cors_allow_credentials
    if "*" in settings.cors_origins and allow_credentials:
        # El navegador rechaza '*' junto con credenciales; se desactivan para no romper.
        logger.warning(
            "CORS_ORIGINS contiene '*' junto con CORS_ALLOW_CREDENTIALS=true; "
            "se deshabilitan las credenciales en CORS"
        )
        allow_credentials = False

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=allow_credentials,
        allow_methods=settings.cors_allow_methods,
        allow_headers=settings.cors_allow_headers,
        expose_headers=[REQUEST_ID_HEADER],
    )


def register_request_context(app: FastAPI) -> None:
    """Asigna un identificador por petición y registra el acceso."""

    @app.middleware("http")
    async def _request_context(request: Request, call_next):
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        set_request_id(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.exception(
                "%s %s falló tras %.0f ms", request.method, request.url.path, elapsed_ms
            )
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers[REQUEST_ID_HEADER] = request_id
        logger.info(
            "%s %s -> %s (%.0f ms)",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response
