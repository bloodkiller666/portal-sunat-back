"""Endpoint público de salud. No exige token ni base de datos."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from app.api.deps import SettingsDep
from app.services.results import HealthResponse

router = APIRouter(tags=["salud"])


@router.get("/health", response_model=HealthResponse, summary="Estado del servicio")
async def health(settings: SettingsDep) -> HealthResponse:
    """Liveness/readiness básico: no consulta SUNAT ni la base de datos."""
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
