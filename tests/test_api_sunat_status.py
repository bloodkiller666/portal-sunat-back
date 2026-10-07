from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.enums import SunatAmbiente, TipoOperacionSunat
from app.secrets.env import EnvSecretsProvider
from tests.conftest import CLAVE_SOL, RUC_AJENA, RUC_PRINCIPAL, USUARIO_ID, USUARIO_SOL
from tests.fakes import (
    FakeSunatClient,
    FakeUnitOfWork,
    disponibilidad_autenticado,
    disponibilidad_credenciales_invalidas,
    disponibilidad_servicio_caido,
)

RUTA = f"/api/v1/companies/{RUC_PRINCIPAL}/sunat/status"


def test_sin_token_devuelve_401(client: TestClient) -> None:
    assert client.get(RUTA).status_code == 401


def test_ruc_con_formato_invalido(client: TestClient, auth: dict[str, str]) -> None:
    respuesta = client.get("/api/v1/companies/123/sunat/status", headers=auth)
    assert respuesta.status_code == 422
    assert respuesta.json()["error"]["code"] == "validation_error"


def test_empresa_inexistente(client: TestClient, auth: dict[str, str]) -> None:
    respuesta = client.get("/api/v1/companies/20111111111/sunat/status", headers=auth)
    assert respuesta.status_code == 404


def test_empresa_de_otro_usuario(client: TestClient, auth: dict[str, str]) -> None:
    respuesta = client.get(f"/api/v1/companies/{RUC_AJENA}/sunat/status", headers=auth)
    assert respuesta.status_code == 403
    assert respuesta.json()["error"]["code"] == "forbidden"


def test_rol_lector_no_puede_probar_credenciales(
    client: TestClient, auth_lector: dict[str, str]
) -> None:
    respuesta = client.get(RUTA, headers=auth_lector)
    assert respuesta.status_code == 403
    assert respuesta.json()["error"]["details"]["rol"] == "lector"


def test_autenticacion_correcta_sin_exponer_secretos(
    client: TestClient, auth: dict[str, str], sunat: FakeSunatClient
) -> None:
    sunat.disponibilidad = disponibilidad_autenticado()

    respuesta = client.get(RUTA, headers=auth)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "autenticado"
    assert cuerpo["autenticado"] is True
    assert cuerpo["metodo"] == "getStatus"
    assert cuerpo["detalle"]["codigo"] == "0"
    assert cuerpo["credenciales"]["sol_user_configurado"] is True
    assert cuerpo["credenciales"]["certificado"]["configurado"] is False
    # Nada de la clave SOL, el usuario SOL o el certificado en la respuesta.
    assert CLAVE_SOL not in respuesta.text
    assert USUARIO_SOL not in respuesta.text
    assert "sol_password" not in respuesta.text


def test_credenciales_invalidas_no_es_un_error_500(
    client: TestClient, auth: dict[str, str], sunat: FakeSunatClient
) -> None:
    sunat.disponibilidad = disponibilidad_credenciales_invalidas()

    respuesta = client.get(RUTA, headers=auth)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "credenciales_invalidas"
    assert cuerpo["autenticado"] is False
    assert cuerpo["detalle"]["codigo"] == "0104"


def test_sunat_caido_se_reporta_como_servicio_no_disponible(
    client: TestClient, auth: dict[str, str], sunat: FakeSunatClient
) -> None:
    sunat.disponibilidad = disponibilidad_servicio_caido()

    respuesta = client.get(RUTA, headers=auth)

    assert respuesta.status_code == 200
    assert respuesta.json()["estado"] == "servicio_no_disponible"


def test_sin_credenciales_configuradas(
    app: FastAPI, client: TestClient, auth: dict[str, str], sunat: FakeSunatClient
) -> None:
    app.state.secrets_provider = EnvSecretsProvider({})

    respuesta = client.get(RUTA, headers=auth)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "sin_credenciales"
    assert cuerpo["credenciales"]["sol_user_configurado"] is False
    assert sunat.llamadas == []  # no se intenta autenticar sin credenciales


def test_registra_el_intento_en_sunat_envios(
    client: TestClient,
    auth: dict[str, str],
    sunat: FakeSunatClient,
    uow: FakeUnitOfWork,
) -> None:
    sunat.disponibilidad = disponibilidad_autenticado()

    cuerpo = client.get(RUTA, headers=auth).json()

    assert cuerpo["intento_id"] == 1
    assert len(uow.sunat_envios.envios) == 1
    intento = uow.sunat_envios.envios[0]
    assert intento.tipo_operacion is TipoOperacionSunat.prueba_conexion
    assert intento.exito is True
    assert intento.sunat_ambiente is SunatAmbiente.beta
    assert intento.duracion_ms == 35
    assert intento.request_xml is not None
    assert "Password" not in intento.request_xml
    assert CLAVE_SOL not in (intento.response_xml or "")


def test_la_bitacora_caida_no_rompe_la_respuesta(
    client: TestClient,
    auth: dict[str, str],
    sunat: FakeSunatClient,
    uow: FakeUnitOfWork,
) -> None:
    sunat.disponibilidad = disponibilidad_autenticado()
    uow.sunat_envios.fallar = True

    respuesta = client.get(RUTA, headers=auth)

    assert respuesta.status_code == 200
    assert respuesta.json()["intento_id"] is None


def test_entrega_las_credenciales_de_la_empresa_al_cliente(
    client: TestClient, auth: dict[str, str], sunat: FakeSunatClient
) -> None:
    sunat.disponibilidad = disponibilidad_autenticado()

    client.get(RUTA, headers=auth)

    assert sunat.credentials is not None
    assert sunat.credentials.ruc == RUC_PRINCIPAL
    assert sunat.credentials.sunat_username == f"{RUC_PRINCIPAL}{USUARIO_SOL}"
    assert sunat.env is SunatAmbiente.beta


def test_la_sesion_usa_los_claims_del_usuario(
    client: TestClient,
    auth: dict[str, str],
    sunat: FakeSunatClient,
    uow: FakeUnitOfWork,
) -> None:
    sunat.disponibilidad = disponibilidad_autenticado()

    client.get(RUTA, headers=auth)

    assert uow.claims["sub"] == str(USUARIO_ID)
