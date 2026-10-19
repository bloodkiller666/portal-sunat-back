"""Endpoint que recupera el XML y el CDR de los comprobantes emitidos desde Bizlinks."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query

from app.api.deps import (
    AcessoOperativo,
    BizlinksClientFactoryDep,
    SecretsProviderDep,
    SettingsDep,
    UowDep,
)
from app.services.bizlinks import LIMITE_MAXIMO, LIMITE_POR_DEFECTO, sincronizar_respaldos
from app.services.results import BizlinksSyncOut

router = APIRouter(tags=["bizlinks"])


@router.post(
    "/companies/{ruc}/bizlinks/respaldos/sync",
    response_model=BizlinksSyncOut,
    summary="Recupera desde Bizlinks el XML y el CDR que falten (respaldo de auditoría)",
)
async def post_sincronizar_respaldos(
    access: AcessoOperativo,
    uow: UowDep,
    settings: SettingsDep,
    secrets_provider: SecretsProviderDep,
    client_factory: BizlinksClientFactoryDep,
    limit: int = Query(
        default=LIMITE_POR_DEFECTO,
        ge=1,
        le=LIMITE_MAXIMO,
        description="Máximo de comprobantes a completar en esta llamada",
    ),
    fecha_desde: date | None = Query(default=None, description="Emisión desde (AAAA-MM-DD)"),
    fecha_hasta: date | None = Query(default=None, description="Emisión hasta (AAAA-MM-DD)"),
) -> BizlinksSyncOut:
    """Completa el respaldo de los comprobantes emitidos (admin/emisor).

    Recorre los emitidos que no tienen XML o CDR archivado, los consulta en Bizlinks,
    valida que el XML sea el del comprobante y los guarda en R2. Es idempotente: lo que
    ya está archivado no se vuelve a pedir. Una caída de Bizlinks o unas credenciales
    rechazadas detienen el lote (`detenido_por`); repita la llamada para continuar.
    """
    return await sincronizar_respaldos(
        uow=uow,
        settings=settings,
        secrets_provider=secrets_provider,
        client_factory=client_factory,
        access=access,
        limit=limit,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
    )
