"""Pruebas del readiness (`/health/ready`), el healthcheck del orquestador."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from tests.conftest import JWT_SECRET

RUTA = "/health/ready"
DSN_FALSO = "postgresql://usuario:clave@127.0.0.1:1/portal"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "environment": "local",
        "supabase_jwt_secret": JWT_SECRET,
        "cors_origins": ["http://localhost:3000"],
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_ready_no_exige_token(client: TestClient) -> None:
    # Sin cabecera Authorization: es una sonda pública de infraestructura.
    assert client.get(RUTA).status_code in (200, 503)


def test_sin_base_de_datos_no_esta_listo(client: TestClient) -> None:
    respuesta = client.get(RUTA)

    assert respuesta.status_code == 503
    cuerpo = respuesta.json()
    assert cuerpo["status"] == "not_ready"
    assert cuerpo["database"] == "not_configured"
    assert cuerpo["auth_configured"] is True


def test_con_base_disponible_esta_listo(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ping_ok(self: object) -> None:
        return None

    monkeypatch.setattr("app.db.pool.Database.ping", ping_ok)
    with TestClient(create_app(_settings(database_url=DSN_FALSO))) as cliente:
        respuesta = cliente.get(RUTA)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["status"] == "ready"
    assert cuerpo["database"] == "ok"
    # La sonda no puede revelar la cadena de conexión ni el secreto.
    assert "postgresql" not in respuesta.text
    assert JWT_SECRET not in respuesta.text


def test_si_la_base_no_responde_devuelve_503(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ping_caido(self: object) -> None:
        raise RuntimeError("sin conexión")

    monkeypatch.setattr("app.db.pool.Database.ping", ping_caido)
    with TestClient(create_app(_settings(database_url=DSN_FALSO))) as cliente:
        respuesta = cliente.get(RUTA)

    assert respuesta.status_code == 503
    assert respuesta.json()["database"] == "unreachable"


def test_sin_verificacion_de_jwt_no_esta_listo(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ping_ok(self: object) -> None:
        return None

    monkeypatch.setattr("app.db.pool.Database.ping", ping_ok)
    settings = _settings(database_url=DSN_FALSO, supabase_jwt_secret=None)
    with TestClient(create_app(settings)) as cliente:
        respuesta = cliente.get(RUTA)

    assert respuesta.status_code == 503
    assert respuesta.json()["auth_configured"] is False
