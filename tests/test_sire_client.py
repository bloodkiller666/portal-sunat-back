"""Pruebas del cliente HTTP de SIRE con SUNAT mockeado (respx; sin red)."""

from __future__ import annotations

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.core.config import Settings
from app.core.errors import (
    CredentialsNotConfiguredError,
    SunatAuthError,
    SunatFaultError,
    SunatUnavailableError,
)
from app.secrets.base import CompanyCredentials
from app.sunat import sire as sire_modulo
from app.sunat.endpoints import (
    SIRE_ARCHIVO_REPORTE_URL,
    SIRE_EXPORTAR_PROPUESTA_URL,
    SIRE_TICKETS_URL,
    SIRE_TOKEN_URL,
)
from app.sunat.sire import SireClient, SirePeriodoSinDatosError

CLIENT_ID = "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb"
CLIENT_SECRET = "client-secret-de-prueba-no-real"
CLAVE_SOL = "clave-sol-de-prueba"
TOKEN = "token-de-prueba-abcdefghijklmnop"

TOKEN_URL = SIRE_TOKEN_URL.format(client_id=CLIENT_ID)
PROPUESTA_URL = SIRE_EXPORTAR_PROPUESTA_URL.format(periodo="202609")


def _credenciales(**cambios: object) -> CompanyCredentials:
    datos: dict[str, object] = {
        "ruc": "20123456789",
        "sol_user": "USUARIO1",
        "sol_password": SecretStr(CLAVE_SOL),
        "client_id": CLIENT_ID,
        "client_secret": SecretStr(CLIENT_SECRET),
    }
    datos.update(cambios)
    return CompanyCredentials(**datos)  # type: ignore[arg-type]


@pytest.fixture
def cliente(settings: Settings) -> SireClient:
    return SireClient(_credenciales(), settings)


def _token_ok() -> httpx.Response:
    return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 3600})


def test_sin_credenciales_de_api_no_se_puede_crear(settings: Settings) -> None:
    with pytest.raises(CredentialsNotConfiguredError):
        SireClient(_credenciales(client_id=None), settings)
    with pytest.raises(CredentialsNotConfiguredError):
        SireClient(_credenciales(client_secret=None), settings)


@respx.mock
def test_el_token_usa_password_grant_scope_sire_y_usuario_con_ruc(cliente: SireClient) -> None:
    ruta = respx.post(TOKEN_URL).mock(return_value=_token_ok())
    respx.get(PROPUESTA_URL).mock(
        return_value=httpx.Response(200, json={"numTicket": "20260300000071"})
    )

    assert cliente.solicitar_propuesta("202609") == "20260300000071"

    cuerpo = ruta.calls.last.request.content.decode()
    assert "grant_type=password" in cuerpo
    assert "scope=https%3A%2F%2Fapi-sire.sunat.gob.pe" in cuerpo
    assert "username=20123456789USUARIO1" in cuerpo


@respx.mock
def test_el_token_se_reutiliza_entre_llamadas(cliente: SireClient) -> None:
    ruta_token = respx.post(TOKEN_URL).mock(return_value=_token_ok())
    ruta = respx.get(PROPUESTA_URL).mock(
        return_value=httpx.Response(200, json={"numTicket": "20260300000071"})
    )

    cliente.solicitar_propuesta("202609")
    cliente.solicitar_propuesta("202609")

    assert ruta_token.call_count == 1
    assert ruta.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"


@respx.mock
def test_un_token_vencido_se_renueva(
    cliente: SireClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ruta_token = respx.post(TOKEN_URL).mock(return_value=_token_ok())
    respx.get(PROPUESTA_URL).mock(
        return_value=httpx.Response(200, json={"numTicket": "20260300000071"})
    )
    cliente.solicitar_propuesta("202609")
    monkeypatch.setattr(sire_modulo.time, "monotonic", lambda: 10**9)

    cliente.solicitar_propuesta("202609")

    assert ruta_token.call_count == 2


@pytest.mark.parametrize("estado", [400, 401, 403])
@respx.mock
def test_credenciales_rechazadas_dan_error_de_autenticacion(
    cliente: SireClient, estado: int
) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(estado, json={"error": "x"}))

    with pytest.raises(SunatAuthError) as error:
        cliente.solicitar_propuesta("202609")

    for secreto in (CLIENT_SECRET, CLAVE_SOL):
        assert secreto not in str(error.value.message) + str(error.value.details)


@respx.mock
def test_seguridad_caida_es_servicio_no_disponible(cliente: SireClient) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(SunatUnavailableError):
        cliente.solicitar_propuesta("202609")


@respx.mock
def test_sin_red_es_servicio_no_disponible(cliente: SireClient) -> None:
    respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectTimeout("timeout"))

    with pytest.raises(SunatUnavailableError):
        cliente.solicitar_propuesta("202609")


@respx.mock
def test_token_sin_access_token_es_un_fallo(cliente: SireClient) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={}))

    with pytest.raises(SunatFaultError):
        cliente.solicitar_propuesta("202609")


@respx.mock
def test_403_de_sire_sugiere_que_no_esta_acogida(cliente: SireClient) -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_ok())
    respx.get(PROPUESTA_URL).mock(return_value=httpx.Response(403))

    with pytest.raises(SunatAuthError) as error:
        cliente.solicitar_propuesta("202609")

    assert "acogida" in error.value.message


@respx.mock
def test_periodo_sin_comprobantes_es_un_caso_propio(cliente: SireClient) -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_ok())
    respx.get(PROPUESTA_URL).mock(
        return_value=httpx.Response(
            422, json={"cod": "422", "errors": [{"cod": "1070", "msg": "Sin comprobantes"}]}
        )
    )

    with pytest.raises(SirePeriodoSinDatosError):
        cliente.solicitar_propuesta("202609")


@respx.mock
def test_otro_422_se_informa_con_su_codigo(cliente: SireClient) -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_ok())
    respx.get(PROPUESTA_URL).mock(
        return_value=httpx.Response(422, json={"errors": [{"cod": "1006", "msg": "Formato"}]})
    )

    with pytest.raises(SunatFaultError) as error:
        cliente.solicitar_propuesta("202609")

    assert error.value.details["codigos"] == ["1006"]


@respx.mock
def test_respuesta_sin_ticket_es_un_fallo(cliente: SireClient) -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_ok())
    respx.get(PROPUESTA_URL).mock(return_value=httpx.Response(200, json={}))

    with pytest.raises(SunatFaultError):
        cliente.solicitar_propuesta("202609")


@respx.mock
def test_sire_caido_es_servicio_no_disponible(cliente: SireClient) -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_ok())
    respx.get(PROPUESTA_URL).mock(return_value=httpx.Response(500, json={"cod": "500"}))

    with pytest.raises(SunatUnavailableError):
        cliente.solicitar_propuesta("202609")


def _registro_ticket(estado: str = "06", **extra: object) -> dict[str, object]:
    registro: dict[str, object] = {
        "numTicket": "20260300000071",
        "perTributario": "202609",
        "codProceso": "10",
        "detalleTicket": {"codEstadoEnvio": estado, "desEstadoEnvio": "Terminado"},
        "archivoReporte": [
            {"codTipoAchivoReporte": "01", "nomArchivoReporte": "LE20514831620202609.zip"}
        ],
    }
    registro.update(extra)
    return registro


@respx.mock
def test_consultar_ticket_terminado_trae_los_archivos(cliente: SireClient) -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_ok())
    ruta = respx.get(SIRE_TICKETS_URL).mock(
        return_value=httpx.Response(200, json={"registros": [_registro_ticket()]})
    )

    estado = cliente.consultar_ticket("20260300000071", "202609")

    assert estado.terminado and not estado.en_proceso
    assert estado.proceso == "10"
    assert [a.nombre for a in estado.archivos] == ["LE20514831620202609.zip"]
    assert estado.archivos[0].tipo == "01"
    parametros = ruta.calls.last.request.url.params
    assert parametros["numTicket"] == "20260300000071"
    assert parametros["perIni"] == parametros["perFin"] == "202609"
    assert parametros["codLibro"] == "140000"


@pytest.mark.parametrize(
    ("codigo", "en_proceso", "con_errores"),
    [("01", True, False), ("02", True, False), ("05", True, False), ("03", False, True)],
)
@respx.mock
def test_estados_del_ticket(
    cliente: SireClient, codigo: str, en_proceso: bool, con_errores: bool
) -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_ok())
    respx.get(SIRE_TICKETS_URL).mock(
        return_value=httpx.Response(
            200, json={"registros": [_registro_ticket(codigo, archivoReporte=[])]}
        )
    )

    estado = cliente.consultar_ticket("20260300000071", "202609")

    assert estado.en_proceso is en_proceso
    assert estado.con_errores is con_errores
    assert not estado.terminado


@respx.mock
def test_ticket_desconocido_es_un_fallo(cliente: SireClient) -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_ok())
    respx.get(SIRE_TICKETS_URL).mock(return_value=httpx.Response(200, json={"registros": []}))

    with pytest.raises(SunatFaultError):
        cliente.consultar_ticket("20260300000071", "202609")


@respx.mock
def test_descargar_archivo_envia_los_parametros_del_manual(cliente: SireClient) -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_ok())
    respx.get(SIRE_TICKETS_URL).mock(
        return_value=httpx.Response(200, json={"registros": [_registro_ticket()]})
    )
    ruta = respx.get(SIRE_ARCHIVO_REPORTE_URL).mock(
        return_value=httpx.Response(200, content=b"PK\x03\x04zip")
    )
    estado = cliente.consultar_ticket("20260300000071", "202609")

    contenido = cliente.descargar_archivo(estado, estado.archivos[0])

    assert contenido == b"PK\x03\x04zip"
    parametros = ruta.calls.last.request.url.params
    assert parametros["nomArchivoReporte"] == estado.archivos[0].nombre
    assert parametros["codTipoArchivoReporte"] == "01"
    assert parametros["perTributario"] == "202609"
    assert parametros["codProceso"] == "10"
    assert parametros["numTicket"] == "20260300000071"
    assert parametros["codLibro"] == "140000"


@respx.mock
def test_el_zip_descargado_tiene_tope_de_tamano(
    cliente: SireClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sire_modulo, "TAMANO_MAXIMO_ZIP", 10)
    respx.post(TOKEN_URL).mock(return_value=_token_ok())
    respx.get(SIRE_TICKETS_URL).mock(
        return_value=httpx.Response(200, json={"registros": [_registro_ticket()]})
    )
    respx.get(SIRE_ARCHIVO_REPORTE_URL).mock(return_value=httpx.Response(200, content=b"x" * 11))
    estado = cliente.consultar_ticket("20260300000071", "202609")

    with pytest.raises(SunatFaultError):
        cliente.descargar_archivo(estado, estado.archivos[0])
