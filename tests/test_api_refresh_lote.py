"""Refresco masivo de los comprobantes pendientes de una empresa."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.enums import EstadoComprobante
from app.core.errors import SunatAuthError, SunatFaultError, SunatUnavailableError
from app.db.models import Company
from app.secrets.env import EnvSecretsProvider
from tests.conftest import RUC_AJENA, RUC_PRINCIPAL
from tests.fakes import (
    FakeSunatClient,
    FakeUnitOfWork,
    construir_comprobante,
    resultado_con_cdr,
    resultado_sin_cdr,
)

RUTA = f"/api/v1/companies/{RUC_PRINCIPAL}/comprobantes/refresh"


@pytest.fixture
def pendientes(empresa_principal: Company, uow: FakeUnitOfWork) -> list:
    creados = [
        construir_comprobante(
            empresa_principal.id, ruc=RUC_PRINCIPAL, correlativo=correlativo
        )
        for correlativo in (1, 2, 3)
    ]
    for comprobante in creados:
        uow.comprobantes.comprobantes[comprobante.id] = comprobante
    return creados


def test_sin_token_devuelve_401(client: TestClient) -> None:
    assert client.post(RUTA).status_code == 401


def test_rol_lector_no_puede_refrescar(client: TestClient, auth_lector: dict[str, str]) -> None:
    respuesta = client.post(RUTA, headers=auth_lector)
    assert respuesta.status_code == 403
    assert respuesta.json()["error"]["details"]["rol"] == "lector"


def test_empresa_ajena(client: TestClient, auth: dict[str, str]) -> None:
    respuesta = client.post(f"/api/v1/companies/{RUC_AJENA}/comprobantes/refresh", headers=auth)
    assert respuesta.status_code == 403


def test_refresca_solo_los_pendientes(
    client: TestClient,
    auth: dict[str, str],
    sunat: FakeSunatClient,
    uow: FakeUnitOfWork,
    empresa_principal: Company,
    pendientes: list,
) -> None:
    aprobado = construir_comprobante(
        empresa_principal.id,
        ruc=RUC_PRINCIPAL,
        correlativo=9,
        estado=EstadoComprobante.aprobado,
    )
    uow.comprobantes.comprobantes[aprobado.id] = aprobado
    sunat.resultado = resultado_con_cdr()

    cuerpo = client.post(RUTA, headers=auth).json()

    assert cuerpo["ruc"] == RUC_PRINCIPAL
    assert cuerpo["total"] == 3
    assert cuerpo["procesados"] == 3
    assert cuerpo["con_cambio"] == 3
    assert cuerpo["fallidos"] == 0
    assert cuerpo["detenido_por"] is None
    assert len(cuerpo["items"]) == 3
    assert all(item["estado_nuevo"] == "aprobado" for item in cuerpo["items"])
    assert all(item["cdr_codigo"] == "0" for item in cuerpo["items"])
    # El aprobado no se consultó a SUNAT.
    assert len(sunat.llamadas) == 3
    # Y cada intento quedó en la bitácora.
    assert len(uow.sunat_envios.envios) == 3


def test_respeta_el_limite_del_lote(
    client: TestClient,
    auth: dict[str, str],
    sunat: FakeSunatClient,
    pendientes: list,
) -> None:
    sunat.resultado = resultado_con_cdr()

    cuerpo = client.post(RUTA, headers=auth, params={"limit": 2}).json()

    assert cuerpo["total"] == 2
    assert cuerpo["procesados"] == 2
    assert len(sunat.llamadas) == 2


def test_sin_pendientes_no_consulta_sunat(
    client: TestClient, auth: dict[str, str], sunat: FakeSunatClient
) -> None:
    cuerpo = client.post(RUTA, headers=auth).json()

    assert cuerpo["total"] == 0
    assert cuerpo["items"] == []
    assert sunat.llamadas == []


def test_un_codigo_en_proceso_no_cambia_el_estado(
    client: TestClient,
    auth: dict[str, str],
    sunat: FakeSunatClient,
    pendientes: list,
) -> None:
    sunat.resultado = resultado_sin_cdr("98", "El comprobante está en proceso")

    cuerpo = client.post(RUTA, headers=auth).json()

    assert cuerpo["procesados"] == 3
    assert cuerpo["con_cambio"] == 0
    assert all(item["estado_nuevo"] == "pendiente" for item in cuerpo["items"])


def test_sunat_caido_detiene_el_lote(
    client: TestClient,
    auth: dict[str, str],
    sunat: FakeSunatClient,
    pendientes: list,
) -> None:
    """No se insiste con los siguientes: cada intento costaría un timeout entero."""
    sunat.error = SunatUnavailableError("No se pudo contactar a SUNAT")

    respuesta = client.post(RUTA, headers=auth)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["detenido_por"] == "sunat_unavailable"
    assert cuerpo["total"] == 3
    assert cuerpo["procesados"] == 1
    assert cuerpo["fallidos"] == 1
    assert cuerpo["items"][0]["ok"] is False
    assert cuerpo["items"][0]["error_code"] == "sunat_unavailable"
    assert len(sunat.llamadas) == 1


def test_credenciales_rechazadas_detienen_el_lote(
    client: TestClient,
    auth: dict[str, str],
    sunat: FakeSunatClient,
    pendientes: list,
) -> None:
    sunat.error = SunatAuthError("SUNAT rechazó las credenciales SOL de la empresa")

    cuerpo = client.post(RUTA, headers=auth).json()

    assert cuerpo["detenido_por"] == "sunat_auth_error"
    assert cuerpo["procesados"] == 1
    assert len(sunat.llamadas) == 1


def test_un_error_puntual_no_detiene_el_lote(
    client: TestClient,
    auth: dict[str, str],
    sunat: FakeSunatClient,
    pendientes: list,
) -> None:
    """Un comprobante problemático no debe impedir refrescar los demás."""
    sunat.error = SunatFaultError("SUNAT devolvió una respuesta inesperada")

    cuerpo = client.post(RUTA, headers=auth).json()

    assert cuerpo["detenido_por"] is None
    assert cuerpo["procesados"] == 3
    assert cuerpo["fallidos"] == 3
    assert len(sunat.llamadas) == 3


def test_valida_el_limite(client: TestClient, auth: dict[str, str]) -> None:
    assert client.post(RUTA, headers=auth, params={"limit": 0}).status_code == 422
    assert client.post(RUTA, headers=auth, params={"limit": 999}).status_code == 422


def test_no_expone_tramas_ni_secretos(
    client: TestClient, auth: dict[str, str], sunat: FakeSunatClient, pendientes: list
) -> None:
    sunat.resultado = resultado_con_cdr()

    respuesta = client.post(RUTA, headers=auth)

    assert "request_xml" not in respuesta.text
    assert "response_xml" not in respuesta.text


def test_sin_credenciales_configuradas_detiene_el_lote(
    app: FastAPI,
    client: TestClient,
    auth: dict[str, str],
    sunat: FakeSunatClient,
    pendientes: list,
) -> None:
    """Sin credenciales SOL no se puede consultar nada: se corta el lote de inmediato."""
    app.state.secrets_provider = EnvSecretsProvider({})

    cuerpo = client.post(RUTA, headers=auth).json()

    assert cuerpo["detenido_por"] == "credentials_not_configured"
    assert cuerpo["procesados"] == 1
    assert sunat.llamadas == []
