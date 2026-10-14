"""Endpoints públicos de salud. No exigen token ni credenciales SUNAT."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.deps import SettingsDep
from app.core.logging import get_logger
from app.db.pool import Database
from app.services.results import HealthResponse, ReadyResponse

router = APIRouter(tags=["salud"])

logger = get_logger(__name__)


@router.get("/health", response_model=HealthResponse, summary="Estado del servicio")
async def health(settings: SettingsDep) -> HealthResponse:
    """Liveness básico: no consulta SUNAT ni la base de datos."""
    return HealthResponse(
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.environment.value,
        sunat_env=settings.sunat_env,
        confirm_prod_emission=settings.confirm_prod_emission,
        database_configured=settings.database_configured,
        auth_configured=settings.auth_configured,
        timestamp=datetime.now(tz=UTC),
    )


@router.get(
    "/health/ready",
    response_model=ReadyResponse,
    summary="Readiness: base de datos y verificación de tokens",
    responses={503: {"description": "La instancia no está lista para recibir tráfico"}},
)
async def ready(request: Request, settings: SettingsDep) -> JSONResponse:
    """Readiness para el orquestador (healthcheck del contenedor).

    Devuelve `503` mientras la base de datos no responda o falte configurar la
    verificación del JWT, para que la plataforma no envíe tráfico a esa instancia.
    Nunca revela la cadena de conexión ni el secreto.
    """
    database: Database = request.app.state.database

    if not settings.database_configured:
        estado_base = "not_configured"
    else:
        try:
            await database.ping()
        except Exception:
            logger.exception("La base de datos no respondió al readiness")
            estado_base = "unreachable"
        else:
            estado_base = "ok"

    listo = estado_base == "ok" and settings.auth_configured
    payload = ReadyResponse(
        status="ready" if listo else "not_ready",
        database=estado_base,
        auth_configured=settings.auth_configured,
        sunat_env=settings.sunat_env,
        timestamp=datetime.now(tz=UTC),
    )
    return JSONResponse(
        status_code=200 if listo else 503,
        content=payload.model_dump(mode="json"),
    )
