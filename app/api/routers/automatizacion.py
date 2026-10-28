"""Apartado «Logs»: corridas de la automatización diaria (SIRE + correo + importación)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, Field

from app.api.deps import CompanyAccessDep, CurrentUserDep, UowDep
from app.services.automatizacion_logs import (
    LIMITE_MAXIMO,
    LIMITE_POR_DEFECTO,
    listar_logs,
    registrar_fallo_del_portal,
)
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
    origen: Annotated[
        Literal["automatizacion", "portal"] | None,
        Query(description="`portal`: fallos que vio el usuario; `automatizacion`: las corridas"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=LIMITE_MAXIMO)] = LIMITE_POR_DEFECTO,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LogsPage:
    """Las más recientes primero. Cada una trae sus novedades, errores y el detalle por tarea;
    `estado` resume dónde va la automatización: última sincronización de SIRE y lo que falta
    importar. Cualquier miembro de la empresa puede verlo, incluido `lector`.
    """
    return await listar_logs(
        uow=uow, access=access, resultado=resultado, origen=origen, limit=limit, offset=offset
    )


class FalloPortalIn(BaseModel):
    """Un fallo que el portal mostró al usuario."""

    accion: str = Field(min_length=1, max_length=60, description="Qué intentaba hacer")
    mensaje: str = Field(min_length=1, max_length=500)
    codigo: str | None = Field(default=None, max_length=60)
    contexto: dict[str, str] = Field(default_factory=dict, max_length=12)


class FalloPortalOut(BaseModel):
    registrado: bool


@router.post(
    "/companies/{ruc}/portal/fallos",
    response_model=FalloPortalOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Registra en Logs un fallo que el portal mostró al usuario",
)
async def post_fallo_portal(
    body: FalloPortalIn,
    access: CompanyAccessDep,
    user: CurrentUserDep,
    uow: UowDep,
) -> FalloPortalOut:
    """El navegador avisa aquí cuando no pudo abrir, importar, exportar o sincronizar, para que
    quede en «Logs» junto a las corridas de la automatización. `registrado=false` si el rol
    (`lector`) no puede escribir en los logs; el usuario igual vio su aviso.
    """
    registrado = await registrar_fallo_del_portal(
        uow=uow,
        access=access,
        user_id=user.id,
        accion=body.accion,
        mensaje=body.mensaje,
        codigo=body.codigo,
        contexto=body.contexto,
    )
    return FalloPortalOut(registrado=registrado)
