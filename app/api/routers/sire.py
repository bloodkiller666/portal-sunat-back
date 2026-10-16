"""Endpoints de SIRE: sincroniza las ventas reales desde la propuesta RVIE."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import (
    AcessoOperativo,
    CompanyAccessDep,
    CurrentUserDep,
    SecretsProviderDep,
    SettingsDep,
    SireClientFactoryDep,
    UowDep,
)
from app.services.results import EvidenciasPage, SireSyncOut
from app.services.sire import listar_evidencias, sincronizar_ventas

router = APIRouter(tags=["sire"])


class SireSyncIn(BaseModel):
    periodo: str = Field(description="Periodo tributario AAAAMM (p. ej. 202609)")
    ticket: str | None = Field(
        default=None,
        description="Ticket devuelto por una llamada anterior que quedó `en_proceso`",
    )


@router.post(
    "/companies/{ruc}/sire/ventas/sync",
    response_model=SireSyncOut,
    summary="Sincroniza las ventas del periodo desde SIRE (RVIE)",
)
async def post_sincronizar_ventas(
    access: AcessoOperativo,
    user: CurrentUserDep,
    uow: UowDep,
    settings: SettingsDep,
    secrets_provider: SecretsProviderDep,
    client_factory: SireClientFactoryDep,
    cuerpo: SireSyncIn,
) -> SireSyncOut:
    """Descarga la propuesta RVIE del periodo y registra los comprobantes (admin/emisor).

    Es idempotente: lo ya registrado no se duplica. Si SUNAT todavía genera el
    archivo responde `estado=en_proceso` con el `ticket`; vuelva a llamar con él.
    """
    return await sincronizar_ventas(
        uow=uow,
        settings=settings,
        secrets_provider=secrets_provider,
        client_factory=client_factory,
        user=user,
        access=access,
        periodo=cuerpo.periodo,
        ticket=cuerpo.ticket,
    )


@router.get(
    "/companies/{ruc}/sire/evidencias",
    response_model=EvidenciasPage,
    summary="Lista los ZIP originales de SIRE archivados (evidencia de auditoría)",
)
async def get_evidencias(
    access: CompanyAccessDep, user: CurrentUserDep, uow: UowDep
) -> EvidenciasPage:
    """Cada sincronización guarda el ZIP tal como lo entregó SUNAT, con su SHA-256.

    Para descargar uno use `POST /archivos/{id}/download-url` (URL firmada temporal).
    """
    return await listar_evidencias(uow=uow, user=user, access=access)
