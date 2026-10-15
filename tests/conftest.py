"""Fixtures compartidas: configuración, credenciales, datos y cliente HTTP."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.enums import RolEmpresa
from app.db.models import Company
from app.main import create_app
from tests.fakes import (
    FakeSireClient,
    FakeStorageProvider,
    FakeSunatClient,
    FakeUnitOfWork,
    construir_empresa,
    construir_membresia,
    fabricar_cliente_sire,
    fabricar_cliente_sunat,
    uow_factory,
)

USUARIO_ID = UUID("11111111-1111-1111-1111-111111111111")
USUARIO_LECTOR_ID = UUID("22222222-2222-2222-2222-222222222222")
RUC_PRINCIPAL = "20123456789"
RUC_AJENA = "20999999999"
JWT_SECRET = "jwt-secret-de-pruebas-no-usar-en-produccion"
CLAVE_SOL = "clave-sol-de-pruebas"
USUARIO_SOL = "MODDATOS"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        app_version="0.1.0",
        environment="local",
        sunat_env="beta",
        confirm_prod_emission=False,
        supabase_jwt_secret=JWT_SECRET,
        supabase_jwt_audience="authenticated",
        cors_origins=["http://localhost:3000"],
        secrets_provider="env",
    )


@pytest.fixture
def entorno_credenciales(monkeypatch: pytest.MonkeyPatch) -> None:
    """Credenciales SOL del RUC principal, inyectadas por entorno (nunca en el repo)."""
    monkeypatch.setenv(f"SUNAT_CRED_{RUC_PRINCIPAL}_SOL_USER", USUARIO_SOL)
    monkeypatch.setenv(f"SUNAT_CRED_{RUC_PRINCIPAL}_SOL_PASSWORD", CLAVE_SOL)


@pytest.fixture
def empresa_principal() -> Company:
    return construir_empresa(ruc=RUC_PRINCIPAL, razon_social="ACME SAC")


@pytest.fixture
def empresa_ajena() -> Company:
    return construir_empresa(ruc=RUC_AJENA, razon_social="OTRA EMPRESA SAC")


@pytest.fixture
def uow(empresa_principal: Company, empresa_ajena: Company) -> FakeUnitOfWork:
    """Datos de prueba: el usuario es emisor en ACME y no pertenece a la otra empresa."""
    unidad = FakeUnitOfWork()
    unidad.companies.empresas = [empresa_principal, empresa_ajena]
    unidad.companies.membresias = [
        construir_membresia(USUARIO_ID, empresa_principal.id, RolEmpresa.emisor),
        construir_membresia(USUARIO_LECTOR_ID, empresa_principal.id, RolEmpresa.lector),
    ]
    # Emula RLS: solo la empresa del usuario es visible.
    unidad.comprobantes.company_ids_visibles = {empresa_principal.id}
    unidad.archivos.company_ids_visibles = {empresa_principal.id}
    return unidad


@pytest.fixture
def sunat() -> FakeSunatClient:
    return FakeSunatClient()


@pytest.fixture
def sire() -> FakeSireClient:
    return FakeSireClient()


@pytest.fixture
def app(
    settings: Settings,
    uow: FakeUnitOfWork,
    sunat: FakeSunatClient,
    sire: FakeSireClient,
    entorno_credenciales: None,
) -> FastAPI:
    aplicacion = create_app(settings)
    aplicacion.state.uow_factory = uow_factory(uow)
    aplicacion.state.sunat_client_factory = fabricar_cliente_sunat(sunat)
    aplicacion.state.sire_client_factory = fabricar_cliente_sire(sire)
    return aplicacion


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as cliente:
        yield cliente


@pytest.fixture
def settings_con_r2() -> Settings:
    """Configuración con el bucket privado configurado (el proveedor va con doble)."""
    return Settings(
        _env_file=None,
        environment="local",
        sunat_env="beta",
        supabase_jwt_secret=JWT_SECRET,
        supabase_jwt_audience="authenticated",
        cors_origins=["http://localhost:3000"],
        r2_account_id="cuenta",
        r2_access_key_id="AKID",
        r2_secret_access_key="SECRET",
        r2_bucket_name="bucket-privado",
    )


@pytest.fixture
def app_r2(
    settings_con_r2: Settings,
    uow: FakeUnitOfWork,
    entorno_credenciales: None,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    """App con R2 configurado y el proveedor de almacenamiento reemplazado."""
    aplicacion = create_app(settings_con_r2)
    aplicacion.state.uow_factory = uow_factory(uow)
    aplicacion.state.sunat_client_factory = fabricar_cliente_sunat(FakeSunatClient())
    monkeypatch.setattr(
        "app.services.store.get_storage_provider", lambda _settings: FakeStorageProvider()
    )
    return aplicacion


@pytest.fixture
def cliente_r2(app_r2: FastAPI) -> Iterator[TestClient]:
    with TestClient(app_r2) as cliente:
        yield cliente


def crear_token(
    *,
    user_id: UUID = USUARIO_ID,
    audience: str = "authenticated",
    secret: str = JWT_SECRET,
    expira_en: int = 3600,
    algoritmo: str = "HS256",
    incluir_sub: bool = True,
    incluir_exp: bool = True,
    email: str = "usuario@example.com",
    issuer: str | None = None,
) -> str:
    ahora = datetime.now(tz=UTC)
    payload: dict[str, object] = {"role": "authenticated", "email": email, "aud": audience}
    if issuer:
        payload["iss"] = issuer
    if incluir_sub:
        payload["sub"] = str(user_id)
    if incluir_exp:
        payload["exp"] = int((ahora + timedelta(seconds=expira_en)).timestamp())
    return jwt.encode(payload, secret, algorithm=algoritmo)


@pytest.fixture
def token() -> str:
    return crear_token()


@pytest.fixture
def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def token_lector() -> str:
    return crear_token(user_id=USUARIO_LECTOR_ID, email="lector@example.com")


@pytest.fixture
def auth_lector(token_lector: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_lector}"}
