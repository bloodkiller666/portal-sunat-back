from __future__ import annotations

import httpx
import pytest
import respx

from app.core.config import Settings
from app.core.enums import SunatAmbiente
from app.core.errors import (
    SunatAuthError,
    SunatFaultError,
    SunatUnavailableError,
)
from app.secrets.base import CompanyCredentials
from app.sunat.consult import SunatConsultClient
from tests.fakes import ENDPOINT_BILL, ENDPOINT_CONSULTA, WSDL_DIR, cargar_cdr_xml, cdr_base64

CLAVE_SOL = "clave-sol-de-pruebas"
RUC = "20123456789"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "sunat_env": "prod",
        "sunat_consult_service_url": ENDPOINT_CONSULTA,
        "sunat_wsdl_dir": WSDL_DIR,
        "sunat_timeout_seconds": 5,
        "sunat_max_retries": 0,
    }
    base.update(overrides)
    return Settings(**base)


def _credenciales() -> CompanyCredentials:
    return CompanyCredentials(ruc=RUC, sol_user="MODDATOS", sol_password=CLAVE_SOL)


def _envelope(cuerpo: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soapenv:Header/><soapenv:Body>"
        f"{cuerpo}"
        "</soapenv:Body></soapenv:Envelope>"
    )


def _respuesta_status_cdr(codigo: str, mensaje: str, contenido: str | None = None) -> str:
    contenido_xml = f"<ns:content>{contenido}</ns:content>" if contenido else ""
    return _envelope(
        '<ns:getStatusCdrResponse xmlns:ns="http://service.sunat.gob.pe">'
        "<ns:statusCdr>"
        f"<ns:statusCode>{codigo}</ns:statusCode>"
        f"<ns:statusMessage>{mensaje}</ns:statusMessage>"
        f"{contenido_xml}"
        "</ns:statusCdr>"
        "</ns:getStatusCdrResponse>"
    )


def _respuesta_status(codigo: str, mensaje: str) -> str:
    return _envelope(
        '<ns:getStatusResponse xmlns:ns="http://service.sunat.gob.pe">'
        "<ns:status>"
        f"<ns:statusCode>{codigo}</ns:statusCode>"
        f"<ns:statusMessage>{mensaje}</ns:statusMessage>"
        "</ns:status>"
        "</ns:getStatusResponse>"
    )


def _respuesta_status_ar(codigo: str, mensaje: str) -> str:
    """Respuesta de `billService.getStatusAR` (la sonda de beta)."""
    return _envelope(
        '<ns:getStatusResponseAR xmlns:ns="http://service.sunat.gob.pe">'
        "<ns:status>"
        f"<ns:statusCode>{codigo}</ns:statusCode>"
        f"<ns:statusMessage>{mensaje}</ns:statusMessage>"
        "</ns:status>"
        "</ns:getStatusResponseAR>"
    )


def _fault(texto: str, codigo: str = "soap-env:Client") -> str:
    return _envelope(
        "<soapenv:Fault>"
        f"<faultcode>{codigo}</faultcode>"
        f"<faultstring>{texto}</faultstring>"
        "</soapenv:Fault>"
    )


@respx.mock
def test_get_status_cdr_interpreta_el_cdr_y_no_filtra_la_clave() -> None:
    ruta = respx.post(ENDPOINT_CONSULTA).mock(
        return_value=httpx.Response(200, text=_respuesta_status_cdr("0", "aceptada", cdr_base64()))
    )
    client = SunatConsultClient(_credenciales(), _settings())

    resultado = client.get_status_cdr(tipo_comprobante="01", serie="F001", numero="00000001")

    assert resultado.estado.codigo == "0"
    assert resultado.estado.tiene_cdr is True
    assert resultado.cdr is not None
    assert resultado.cdr.codigo == "0"
    assert "aceptada" in resultado.cdr.descripcion
    assert resultado.cdr.notas
    assert resultado.cdr.archivo == "R-20123456789-01-F001-1.xml"
    assert resultado.tramas.endpoint == ENDPOINT_CONSULTA
    assert resultado.tramas.http_status == 200

    # La credencial viaja en la trama real (WS-Security)...
    enviado = ruta.calls.last.request.content.decode()
    assert RUC + "MODDATOS" in enviado
    assert CLAVE_SOL in enviado
    # ... pero la trama que se persiste/loguea viene saneada.
    assert resultado.tramas.request_xml is not None
    assert CLAVE_SOL not in resultado.tramas.request_xml
    assert "Password" not in resultado.tramas.request_xml
    assert "Security" not in resultado.tramas.request_xml


@respx.mock
def test_get_status_cdr_sin_cdr_disponible() -> None:
    respx.post(ENDPOINT_CONSULTA).mock(
        return_value=httpx.Response(200, text=_respuesta_status_cdr("98", "en proceso"))
    )
    client = SunatConsultClient(_credenciales(), _settings())

    resultado = client.get_status_cdr(tipo_comprobante="01", serie="F001", numero="00000001")

    assert resultado.estado.codigo == "98"
    assert resultado.estado.tiene_cdr is False
    assert resultado.cdr is None


@respx.mock
def test_no_envia_monto_total_y_manda_el_numero_entero() -> None:
    ruta = respx.post(ENDPOINT_CONSULTA).mock(
        return_value=httpx.Response(200, text=_respuesta_status_cdr("0", "aceptada"))
    )
    client = SunatConsultClient(_credenciales(), _settings())

    client.get_status_cdr(tipo_comprobante="01", serie="F001", numero="00000001")
    body = ruta.calls.last.request.content.decode()
    # El WSDL real de producción no declara montoTotal.
    assert "montoTotal" not in body
    assert "numeroComprobante" in body
    assert "F001" in body


@respx.mock
def test_cdr_no_interpretable_no_tumba_la_consulta() -> None:
    respx.post(ENDPOINT_CONSULTA).mock(
        return_value=httpx.Response(
            200, text=_respuesta_status_cdr("0", "aceptada", "bm8tZXMtdW4temlw")
        )
    )
    client = SunatConsultClient(_credenciales(), _settings())

    resultado = client.get_status_cdr(tipo_comprobante="01", serie="F001", numero="00000001")

    assert resultado.cdr is None
    assert resultado.error_cdr is not None
    assert resultado.estado.tiene_cdr is True


@respx.mock
def test_fault_de_autenticacion() -> None:
    respx.post(ENDPOINT_CONSULTA).mock(
        return_value=httpx.Response(401, text=_fault("El Usuario no autorizado"))
    )
    client = SunatConsultClient(_credenciales(), _settings())

    with pytest.raises(SunatAuthError) as error:
        client.get_status_cdr(tipo_comprobante="01", serie="F001", numero="00000001")

    assert CLAVE_SOL not in error.value.message
    assert client.last_tramas is not None
    assert CLAVE_SOL not in (client.last_tramas.request_xml or "")


@respx.mock
def test_fault_por_clave_incorrecta() -> None:
    respx.post(ENDPOINT_CONSULTA).mock(
        return_value=httpx.Response(500, text=_fault("0104 La clave ingresada es incorrecta"))
    )
    client = SunatConsultClient(_credenciales(), _settings())

    with pytest.raises(SunatAuthError):
        client.get_status(tipo_comprobante="01", serie="F001", numero="00000001")


@respx.mock
def test_fault_de_comprobante_no_disponible() -> None:
    respx.post(ENDPOINT_CONSULTA).mock(
        return_value=httpx.Response(
            500, text=_fault("1033 El comprobante no existe", "soap-env:Server")
        )
    )
    client = SunatConsultClient(_credenciales(), _settings())

    with pytest.raises(SunatFaultError) as error:
        client.get_status_cdr(tipo_comprobante="01", serie="F001", numero="00000001")

    assert error.value.details.get("no_disponible") is True
    assert error.value.details.get("codigo") == "1033"


@respx.mock
def test_fault_de_dominio_generico() -> None:
    respx.post(ENDPOINT_CONSULTA).mock(
        return_value=httpx.Response(500, text=_fault("El XML está mal formado", "soap-env:Server"))
    )
    client = SunatConsultClient(_credenciales(), _settings())

    with pytest.raises(SunatFaultError) as error:
        client.get_status_cdr(tipo_comprobante="01", serie="F001", numero="00000001")

    assert error.value.details.get("no_disponible") is not True


@respx.mock
def test_timeout_de_red() -> None:
    respx.post(ENDPOINT_CONSULTA).mock(side_effect=httpx.ConnectTimeout("timeout"))
    client = SunatConsultClient(_credenciales(), _settings())

    with pytest.raises(SunatUnavailableError, match="timeout o error de red"):
        client.get_status_cdr(tipo_comprobante="01", serie="F001", numero="00000001")


@respx.mock
def test_estado_http_inesperado() -> None:
    respx.post(ENDPOINT_CONSULTA).mock(return_value=httpx.Response(503, text="<html>caído</html>"))
    client = SunatConsultClient(_credenciales(), _settings())

    with pytest.raises(SunatUnavailableError, match="503"):
        client.get_status_cdr(tipo_comprobante="01", serie="F001", numero="00000001")


def test_parametros_que_no_existen_en_el_wsdl() -> None:
    """Un WSDL distinto al esperado debe reportarse como error claro, no como 500."""
    client = SunatConsultClient(_credenciales(), _settings())
    with pytest.raises(SunatFaultError, match="no acepta los parámetros"):
        client._invocar("getStatusCdr", {"parametroInexistente": "x"})


@respx.mock
def test_prueba_de_disponibilidad_en_produccion_usa_billconsultservice() -> None:
    ruta = respx.post(ENDPOINT_CONSULTA).mock(
        return_value=httpx.Response(200, text=_respuesta_status("0", "El comprobante no existe"))
    )
    client = SunatConsultClient(_credenciales(), _settings())

    resultado = client.check_availability()

    assert resultado.autenticado is True
    assert resultado.metodo == "getStatus"
    assert resultado.endpoint == ENDPOINT_CONSULTA
    assert ruta.called


@respx.mock
def test_prueba_de_disponibilidad_en_beta_usa_billservice() -> None:
    settings = _settings(sunat_env=SunatAmbiente.beta, sunat_bill_service_url=ENDPOINT_BILL)
    ruta = respx.post(ENDPOINT_BILL).mock(
        return_value=httpx.Response(200, text=_respuesta_status_ar("0", "El archivo no existe"))
    )
    client = SunatConsultClient(_credenciales(), settings)

    resultado = client.check_availability()

    assert resultado.autenticado is True
    assert resultado.metodo == "getStatusAR"
    assert resultado.endpoint == ENDPOINT_BILL
    assert resultado.codigo == "0"
    # La sonda consulta por documento, no por nombre de archivo.
    enviado = ruta.calls.last.request.content.decode()
    assert "getStatusAR" in enviado
    assert RUC in enviado
    assert "F001" in enviado


@respx.mock
def test_prueba_de_disponibilidad_con_credenciales_invalidas() -> None:
    respx.post(ENDPOINT_BILL).mock(
        return_value=httpx.Response(401, text=_fault("Usuario no autorizado"))
    )
    settings = _settings(sunat_env=SunatAmbiente.beta, sunat_bill_service_url=ENDPOINT_BILL)

    resultado = SunatConsultClient(_credenciales(), settings).check_availability()

    assert resultado.autenticado is False
    assert resultado.error_tipo == "sunat_auth_error"


@respx.mock
def test_prueba_de_disponibilidad_con_servicio_caido() -> None:
    respx.post(ENDPOINT_BILL).mock(side_effect=httpx.ConnectError("sin red"))
    settings = _settings(sunat_env=SunatAmbiente.beta, sunat_bill_service_url=ENDPOINT_BILL)

    resultado = SunatConsultClient(_credenciales(), settings).check_availability()

    assert resultado.autenticado is False
    assert resultado.error_tipo == "sunat_unavailable"


@respx.mock
def test_fault_de_dominio_en_la_prueba_implica_credenciales_validas() -> None:
    respx.post(ENDPOINT_BILL).mock(
        return_value=httpx.Response(
            500, text=_fault("1033 El comprobante no existe", "soap-env:Server")
        )
    )
    settings = _settings(sunat_env=SunatAmbiente.beta, sunat_bill_service_url=ENDPOINT_BILL)

    resultado = SunatConsultClient(_credenciales(), settings).check_availability()

    assert resultado.autenticado is True
    assert resultado.error_tipo == "sunat_no_disponible"


def test_el_wsdl_de_pruebas_existe() -> None:
    assert (WSDL_DIR / "billConsultService.wsdl").exists()
    assert cargar_cdr_xml("cdr_aceptado.xml")
