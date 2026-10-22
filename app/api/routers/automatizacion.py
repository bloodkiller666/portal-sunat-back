"""Apartado «Logs»: corridas de la automatización diaria (SIRE + correo + importación)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query

from app.api.deps import CompanyAccessDep, UowDep
from app.services.automatizacion_logs import LIMITE_MAXIMO, LIMITE_POR_DEFECTO, listar_logs
from app.services.results import LogsPage

router = APIRouter(tags=["automatizacion"])


@router.get(
    "/companies/{ruc}/automatizacion/logs",
    response_model=LogsPage,
    summary="Corridas de la automatización diaria y su estado (como en Evidencias SIRE)",
)
async def get_logs(
    access: CompanyAccessDep,
    uow: UowDep,
    resultado: Annotated[
        Literal["ok", "con_novedades", "con_errores"] | None,
        Query(description="Solo las corridas con ese resultado"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=LIMITE_MAXIMO)] = LIMITE_POR_DEFECTO,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LogsPage:
    """Las más recientes primero. Cada una trae sus novedades, errores y el detalle por tarea;
    `estado` resume dónde va la automatización: última sincronización de SIRE y lo que falta
    importar. Cualquier miembro de la empresa puede verlo, incluido `lector`.
    """
    return await listar_logs(
        uow=uow, access=access, resultado=resultado, limit=limit, offset=offset
    )
