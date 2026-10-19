"""Servicios de empresas: listado y control de acceso por RUC/rol."""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from app.core.enums import RolEmpresa
from app.core.errors import AuthorizationError, NotFoundError
from app.core.security import AuthenticatedUser
from app.db.models import CompanyMember
from app.db.repositories.base import UnitOfWork
from app.services.results import CompaniesResponse, CompanyOut


def to_company_out(member: CompanyMember) -> CompanyOut:
    return CompanyOut(
        id=member.company.id,
        ruc=member.company.ruc,
        razon_social=member.company.razon_social,
        nombre_comercial=member.company.nombre_comercial,
        activo=member.company.activo,
        sunat_ambiente=member.company.sunat_ambiente,
        rol=member.rol,
    )


async def list_companies(uow: UnitOfWork, user: AuthenticatedUser) -> CompaniesResponse:
    """Empresas del usuario autenticado (la RLS ya limita las filas)."""
    miembros = await uow.companies.list_for_user(user.id)
    items = [to_company_out(miembro) for miembro in miembros]
    return CompaniesResponse(items=items, total=len(items))


async def resolve_company_access(
    uow: UnitOfWork, user: AuthenticatedUser, ruc: str
) -> CompanyMember:
    """Devuelve la empresa + rol del usuario o falla con 404/403."""
    acceso = await uow.companies.get_access(ruc, user.id)
    if acceso is None:
        raise NotFoundError("La empresa no existe")

    company, membership = acceso
    if membership is None or not membership.activo:
        raise AuthorizationError("No pertenece a esta empresa")

    return CompanyMember(company=company, rol=membership.rol)


async def require_membership(
    uow: UnitOfWork,
    user: AuthenticatedUser,
    company_id: UUID,
    *,
    roles: Iterable[RolEmpresa] | None = None,
) -> CompanyMember:
    """Verifica la pertenencia (y el rol) del usuario sobre una empresa."""
    company = await uow.companies.get_by_id(company_id)
    if company is None:
        raise NotFoundError("La empresa no existe")

    membership = await uow.companies.get_membership(user.id, company_id)
    if membership is None or not membership.activo:
        raise AuthorizationError("No pertenece a esta empresa")

    if roles is not None and membership.rol not in set(roles):
        raise AuthorizationError(
            "Su rol en esta empresa no permite realizar esta operación",
            details={"rol": membership.rol.value},
        )

    return CompanyMember(company=company, rol=membership.rol)
