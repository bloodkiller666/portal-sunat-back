"""Endpoint de sincronización del CDR de un comprobante ya emitido."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Path

from app.api.deps import (
    CurrentUserDep,
    SecretsProviderDep,
    SettingsDep,
    SunatClientFactoryDep,
    UowDep,
)
from app.services.comprobantes import refresh_cdr
from app.services.results import RefreshCdrOut

router = APIRouter(tags=["comprobantes"])


@router.post(
    "/comprobantes/{comprobante_id}/refresh-cdr",
    response_model=RefreshCdrOut,
    summary="Consulta SUNAT y actualiza el estado del comprobante",
)
async def post_refresh_cdr(
    user: CurrentUserDep,
    uow: UowDep,
    settings: SettingsDep,
    secrets_provider: SecretsProviderDep,
    client_factory: SunatClientFactoryDep,
    comprobante_id: UUID = Path(description="Identificador del comprobante"),
) -> RefreshCdrOut:
    """Consulta el estado/CDR en SUNAT, actualiza `comprobantes` y registra el intento.

    - Si SUNAT todavía no conoce el comprobante, el estado queda en Pendiente.
    - Si SUNAT no responde, se devuelve 503 y el intento queda registrado.
    - Una consulta nunca revierte el estado Anulado.
    """
    return await refresh_cdr(
        uow=uow,
        settings=settings,
        secrets_provider=secrets_provider,
        client_factory=client_factory,
        user=user,
        comprobante_id=comprobante_id,
    )
