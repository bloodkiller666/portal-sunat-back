from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from tests.conftest import RUC_PRINCIPAL, USUARIO_ID, crear_token
from tests.fakes import (
    FakeSunatClient,
    FakeUnitOfWork,
    fabricar_cliente_sunat,
    uow_factory,
)


def test_sin_token_devuelve_401(client: TestClient) -> None:
    respuesta = client.get("/api/v1/companies")
    assert respuesta.status_code == 401
    assert respuesta.headers["WWW-Authenticate"] == "Bearer"
    assert respuesta.json()["error"]["code"] == "unauthenticated"
    # El mensaje debe ser legible: el redactor no puede comerse el texto de ayuda.
    assert "redactado" not in respuesta.json()["error"]["message"]


def test_token_invalido_devuelve_401(client: TestClient) -> None:
    respuesta = client.get("/api/v1/companies", headers={"Authorization": "Bearer no-es-un-jwt"})
    assert respuesta.status_code == 401


def test_token_expirado_devuelve_401(client: TestClient) -> None:
    expirado = crear_token(expira_en=-60)
    respuesta = client.get("/api/v1/companies", headers={"Authorization": f"Bearer {expirado}"})
    assert respuesta.status_code == 401
    assert "expiró" in respuesta.json()["error"]["message"]


def test_lista_solo_las_empresas_del_usuario(client: TestClient, auth: dict[str, str]) -> None:
    respuesta = client.get("/api/v1/companies", headers=auth)
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["total"] == 1
    empresa = cuerpo["items"][0]
    assert empresa["ruc"] == RUC_PRINCIPAL
    assert empresa["razon_social"] == "ACME SAC"
    assert empresa["rol"] == "emisor"


def test_la_sesion_lleva_los_claims_del_usuario(
    client: TestClient, uow: FakeUnitOfWork, auth: dict[str, str]
) -> None:
    client.get("/api/v1/companies", headers=auth)
    # Los claims se pasan a Postgres para que RLS evalúe auth.uid().
    assert uow.claims["sub"] == str(USUARIO_ID)
    assert uow.claims["role"] == "authenticated"


def test_comprobantes_no_forman_parte_de_este_listado(
    client: TestClient, auth: dict[str, str]
) -> None:
    cuerpo = client.get("/api/v1/companies", headers=auth).json()
    assert "comprobantes" not in cuerpo


def test_sin_verificacion_de_jwt_configurada_debe_ser_503(
    uow: FakeUnitOfWork, sunat: FakeSunatClient
) -> None:
    """Un servidor sin SUPABASE_JWT_SECRET no puede autenticar: es un 503, no un 500."""
    app = create_app(
        Settings(
            _env_file=None,
            supabase_jwt_secret="",
            cors_origins=["http://localhost:3000"],
        )
    )
    app.state.uow_factory = uow_factory(uow)
    app.state.sunat_client_factory = fabricar_cliente_sunat(sunat)

    with TestClient(app) as cliente:
        sin_token = cliente.get("/api/v1/companies")
        assert sin_token.status_code == 503
        assert sin_token.json()["error"]["code"] == "auth_not_configured"

        con_token = cliente.get(
            "/api/v1/companies", headers={"Authorization": "Bearer cualquier.cosa"}
        )
        assert con_token.status_code == 503
        assert con_token.json()["error"]["code"] == "auth_not_configured"

        # /health sigue disponible y lo reporta para poder diagnosticarlo.
        assert cliente.get("/health").json()["auth_configured"] is False


def test_token_mal_formado_con_auth_configurada_devuelve_401(
    app: FastAPI, uow: FakeUnitOfWork
) -> None:
    assert app.state.settings.auth_configured is True
    with TestClient(app) as cliente:
        respuesta = cliente.get(
            "/api/v1/companies", headers={"Authorization": "Bearer abc.def.ghi"}
        )
    assert respuesta.status_code == 401
    assert respuesta.json()["error"]["code"] == "unauthenticated"
