"""Endpoints de empresas: listado y prueba de conexión con SUNAT."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import (
    CurrentUserDep,
    SecretsProviderDep,
    SettingsDep,
    SunatClientFactoryDep,
    UowDep,
    require_roles,
)
from app.core.enums import RolEmpresa
from app.db.models import CompanyMember
from app.services.companies import list_companies
from app.services.results import CompaniesResponse, SunatStatusOut
from app.services.sunat_status import check_company_sunat_status

router = APIRouter(tags=["empresas"])

AcessoOperativo = Annotated[
    CompanyMember, Depends(require_roles(RolEmpresa.admin, RolEmpresa.emisor))
]


@router.get(
    "/companies",
    response_model=CompaniesResponse,
    summary="Empresas del usuario autenticado",
)
async def get_companies(user: CurrentUserDep, uow: UowDep) -> CompaniesResponse:
    """Devuelve las empresas a las que pertenece el usuario y su rol en cada una."""
    return await list_companies(uow, user)


@router.get(
    "/companies/{ruc}/sunat/status",
    response_model=SunatStatusOut,
    summary="Prueba de conexión y credenciales SUNAT de la empresa",
)
async def get_sunat_status(
    access: AcessoOperativo,
    settings: SettingsDep,
    secrets_provider: SecretsProviderDep,
    client_factory: SunatClientFactoryDep,
    user: CurrentUserDep,
    uow: UowDep,
) -> SunatStatusOut:
    """Ejecuta una consulta de solo lectura contra SUNAT para validar credenciales.

    Nunca devuelve la clave SOL, el usuario SOL ni el contenido del certificado:
    solo si autenticó, el código/mensaje de SUNAT y los metadatos del certificado.
    """
    return await check_company_sunat_status(
        uow=uow,
        settings=settings,
        secrets_provider=secrets_provider,
        client_factory=client_factory,
        user=user,
        access=access,
    )
