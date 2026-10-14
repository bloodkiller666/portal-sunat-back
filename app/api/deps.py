"""Dependencias de FastAPI: configuración, usuario autenticado, UoW y servicios.

Se leen desde `app.state` para que las pruebas puedan inyectar dobles sin tocar
la base de datos ni la red.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Annotated

from fastapi import Depends, Path, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings
from app.core.enums import RolEmpresa
from app.core.errors import (
    AuthenticationError,
    AuthNotConfiguredError,
    AuthorizationError,
    ConfigError,
)
from app.core.security import AuthenticatedUser, JwtVerifier
from app.db.models import CompanyMember
from app.db.repositories.base import UnitOfWork
from app.secrets.base import SecretsProvider
from app.services.companies import resolve_company_access

bearer_scheme = HTTPBearer(
    auto_error=False,
    description="JWT de Supabase Auth (header Authorization: Bearer <token>)",
)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_jwt_verifier(request: Request) -> JwtVerifier:
    return request.app.state.jwt_verifier


def get_secrets_provider(request: Request) -> SecretsProvider:
    return request.app.state.secrets_provider


def get_sunat_client_factory(request: Request) -> Callable:
    return request.app.state.sunat_client_factory


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    verifier: Annotated[JwtVerifier, Depends(get_jwt_verifier)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AuthenticatedUser:
    """Valida el JWT de Supabase. Todo endpoint salvo /health depende de esto."""
    # Sin configuración de servidor no se puede autenticar a nadie: 503 (no 401)
    # para que el problema se vea como configuración y no como token inválido.
    if not settings.auth_configured:
        raise AuthNotConfiguredError()

    if credentials is None or not credentials.credentials:
        # Redacción deliberada: no se escribe `Authorization: Bearer ...` en el
        # mensaje porque el propio filtro de logs lo enmascararía.
        raise AuthenticationError(
            "Falta el token de acceso. Envíe el encabezado Authorization con el esquema Bearer"
        )
    try:
        return await verifier.verify(credentials.credentials)
    except ConfigError as exc:
        raise AuthNotConfiguredError(details={"detalle": exc.message}) from exc


async def get_uow(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(get_current_user)],
) -> AsyncIterator[UnitOfWork]:
    """Abre una sesión de base de datos con los claims del usuario (RLS activo)."""
    factory = request.app.state.uow_factory
    async with factory(user.claims_para_rls) as uow:
        yield uow


async def get_company_access(
    ruc: Annotated[str, Path(pattern=r"^[0-9]{11}$", description="RUC de la empresa")],
    user: Annotated[AuthenticatedUser, Depends(get_current_user)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
) -> CompanyMember:
    """Resuelve la empresa del RUC y exige que el usuario pertenezca a ella."""
    return await resolve_company_access(uow, user, ruc)


def require_roles(*roles: RolEmpresa) -> Callable[..., object]:
    """Dependencia que exige uno de los roles indicados dentro de la empresa."""

    async def _dependency(
        access: Annotated[CompanyMember, Depends(get_company_access)],
    ) -> CompanyMember:
        if access.rol not in set(roles):
            raise AuthorizationError(
                "Su rol en esta empresa no permite realizar esta operación",
                details={"rol": access.rol.value},
            )
        return access

    return _dependency


SettingsDep = Annotated[Settings, Depends(get_settings)]
CurrentUserDep = Annotated[AuthenticatedUser, Depends(get_current_user)]
UowDep = Annotated[UnitOfWork, Depends(get_uow)]
SecretsProviderDep = Annotated[SecretsProvider, Depends(get_secrets_provider)]
SunatClientFactoryDep = Annotated[Callable, Depends(get_sunat_client_factory)]

# Acceso a la empresa del RUC de la ruta (404 si no existe; 403 si no es miembro).
CompanyAccessDep = Annotated[CompanyMember, Depends(get_company_access)]
# Acceso operativo: además exige rol admin o emisor.
AcessoOperativo = Annotated[
    CompanyMember, Depends(require_roles(RolEmpresa.admin, RolEmpresa.emisor))
]
