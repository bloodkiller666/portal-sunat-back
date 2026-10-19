"""Bizlinks (OSE): cliente SOAP, descarga segura y recuperación de XML y CDR."""

from __future__ import annotations

import base64
from datetime import date
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.enums import EstadoComprobante, TipoArchivo
from app.core.errors import (
    BizlinksAuthError,
    BizlinksError,
    BizlinksNotConfiguredError,
    BizlinksUnavailableError,
    StorageNotConfiguredError,
)
from app.db.models import Company
from app.secrets.base import CompanyCredentials
from app.sunat.bizlinks import (
    BizlinksClient,
    DocumentoBizlinks,
    RespaldosBizlinks,
    construir_consulta,
    construir_sobre,
    host_permitido,
    parse_respuesta,
)
from tests.conftest import RUC_PRINCIPAL
from tests.fakes import (
    FakeSireClient,
    FakeUnitOfWork,
    cargar_cdr_xml,
    construir_comprobante,
    construir_zip_propuesta,
    empaquetar_cdr_zip,
    fila_propuesta,
)

UBL = Path(__file__).parent / "fixtures" / "ubl"
ENDPOINT = "https://testing.bizlinks.com.pe/integrador21/ws/invoker"
URL_XML = "https://files.bizlinks.com.pe/sfeperufiles/files/x-xml"
URL_CDR = "https://files.bizlinks.com.pe/sfeperufiles/files/x-cdr"
RUTA = f"/api/v1/companies/{RUC_PRINCIPAL}/bizlinks/respaldos/sync"

RESPUESTA_EBIZ = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<ebizResponse>
  <genericInvokeResponse>
    <commonResponse>
      <summaryResult><total>1</total></summaryResult>
      <status>OK</status>
      <descriptionStatus>Succesfull</descriptionStatus>
    </commonResponse>
    <xmlResult>
      <document>
        <typeDocument>01</typeDocument>
        <idDocument>F001-00000001</idDocument>
        <status>SIGNED</status>
        <statusDocument>EM_01</statusDocument>
        <statusSunat>AC_03</statusSunat>
        <messageSunat><![CDATA[{{"codigo":"0","mensaje":"aceptada"}}]]></messageSunat>
        <pdfFileUrl>https://files.bizlinks.com.pe/x-pdf</pdfFileUrl>
        <xmlFileSignUrl>{URL_XML}</xmlFileSignUrl>
        <xmlFileSunatUrl>{URL_CDR}</xmlFileSunatUrl>
        <numeroDocumentoEmisor>{RUC_PRINCIPAL}</numeroDocumentoEmisor>
      </document>
    </xmlResult>
  </genericInvokeResponse>
</ebizResponse>"""


def _sobre_soap(ebiz: str) -> str:
    """La respuesta real: el ebizResponse viaja como texto dentro de `return`.

    Se escapa (en vez de CDATA) porque el XML interno ya trae CDATA propios.
    """
    return (
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
        '<ns2:invokeResponse xmlns:ns2="http://ws.ce.ebiz.com/">'
        f"<return>{escape(ebiz)}</return>"
        "</ns2:invokeResponse></soap:Body></soap:Envelope>"
    )


@pytest.fixture
def credenciales() -> CompanyCredentials:
    return CompanyCredentials(
        ruc=RUC_PRINCIPAL,
        sol_user="MODDATOS",
        sol_password="clave-sol",
        bizlinks_user="usuario-biz",
        bizlinks_password="clave-bizlinks-secreta",
    )


@pytest.fixture
def settings_bizlinks() -> Settings:
    return Settings(_env_file=None, environment="local", bizlinks_service_url=ENDPOINT)


@pytest.fixture
def cliente(credenciales: CompanyCredentials, settings_bizlinks: Settings) -> BizlinksClient:
    return BizlinksClient(credenciales, settings_bizlinks)


# --------------------------------------------------------------------------- #
# Mensajes
# --------------------------------------------------------------------------- #
def test_la_consulta_lleva_los_parametros_del_manual() -> None:
    comando = construir_consulta(
        ruc=RUC_PRINCIPAL, tipo_documento="01", serie="F001", desde="00000001", hasta="00000001"
    )
    sobre = construir_sobre(comando).decode()

    assert '<parameter value="F001" name="serieGrupoDocumento"/>' in sobre
    assert '<parameter value="00000001" name="numeroCorrelativoInicio"/>' in sobre
    assert f'<parameter value="{RUC_PRINCIPAL}" name="idEmisor"/>' in sobre
    assert "<command><![CDATA[<ConsultCmd" in sobre
    assert 'xmlns:ws="http://ws.ce.ebiz.com/"' in sobre


def test_se_interpreta_la_respuesta_con_las_urls() -> None:
    (documento,) = parse_respuesta(_sobre_soap(RESPUESTA_EBIZ).encode())

    assert documento == DocumentoBizlinks(
        tipo_documento="01",
        id_documento="F001-00000001",
        status="SIGNED",
        status_documento="EM_01",
        status_sunat="AC_03",
        mensaje_sunat='{"codigo":"0","mensaje":"aceptada"}',
        xml_firmado_url=URL_XML,
        xml_sunat_url=URL_CDR,
        pdf_url="https://files.bizlinks.com.pe/x-pdf",
    )


def test_una_consulta_sin_documentos_devuelve_lista_vacia() -> None:
    vacio = RESPUESTA_EBIZ.split("<xmlResult>")[0] + "</genericInvokeResponse></ebizResponse>"

    assert parse_respuesta(_sobre_soap(vacio).encode()) == []


def test_un_estado_distinto_de_ok_es_un_error() -> None:
    ebiz = RESPUESTA_EBIZ.replace("<status>OK</status>", "<status>ERROR</status>")

    with pytest.raises(BizlinksError) as error:
        parse_respuesta(_sobre_soap(ebiz).encode())

    assert error.value.details["estado"] == "ERROR"


def test_un_soap_fault_es_un_error_legible() -> None:
    fault = (
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
        "<soap:Fault><faultcode>s</faultcode><faultstring>fallo interno</faultstring>"
        "</soap:Fault></soap:Body></soap:Envelope>"
    )

    with pytest.raises(BizlinksError) as error:
        parse_respuesta(fault.encode())

    assert error.value.details["detalle"] == "fallo interno"


def test_una_respuesta_que_no_es_xml_se_rechaza() -> None:
    with pytest.raises(BizlinksError):
        parse_respuesta(b"<html>502</html")


# --------------------------------------------------------------------------- #
# Cliente
# --------------------------------------------------------------------------- #
@respx.mock
def test_consultar_envia_basic_auth_y_devuelve_los_documentos(cliente: BizlinksClient) -> None:
    ruta = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, text=_sobre_soap(RESPUESTA_EBIZ))
    )

    (documento,) = cliente.consultar(tipo_documento="01", serie="F001", correlativo_desde=1)

    assert documento.xml_sunat_url == URL_CDR
    peticion = ruta.calls.last.request
    esperado = base64.b64encode(b"usuario-biz:clave-bizlinks-secreta").decode()
    assert peticion.headers["Authorization"] == f"Basic {esperado}"
    assert b"numeroCorrelativoFin" in peticion.content


@respx.mock
@pytest.mark.parametrize(
    ("estado", "excepcion"),
    [(401, BizlinksAuthError), (403, BizlinksAuthError), (503, BizlinksUnavailableError)],
)
def test_errores_http_se_traducen(
    cliente: BizlinksClient, estado: int, excepcion: type[Exception]
) -> None:
    respx.post(ENDPOINT).mock(return_value=httpx.Response(estado, text="x"))

    with pytest.raises(excepcion):
        cliente.consultar(tipo_documento="01", serie="F001", correlativo_desde=1)


@respx.mock
def test_un_fallo_de_red_agota_los_reintentos(
    credenciales: CompanyCredentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.sunat.bizlinks.time.sleep", lambda _s: None)
    cliente = BizlinksClient(
        credenciales,
        Settings(_env_file=None, bizlinks_service_url=ENDPOINT, sunat_max_retries=1),
    )
    ruta = respx.post(ENDPOINT).mock(side_effect=httpx.ConnectError("sin red"))

    with pytest.raises(BizlinksUnavailableError):
        cliente.consultar(tipo_documento="01", serie="F001", correlativo_desde=1)

    assert ruta.call_count == 2


@pytest.mark.parametrize("serie", ["F0", "F001'", "f<01", ""])
def test_los_valores_se_validan_antes_de_armar_el_comando(
    cliente: BizlinksClient, serie: str
) -> None:
    with pytest.raises(BizlinksError):
        cliente.consultar(tipo_documento="01", serie=serie, correlativo_desde=1)


def test_sin_credenciales_de_bizlinks_no_se_construye_el_cliente(
    settings_bizlinks: Settings,
) -> None:
    sin = CompanyCredentials(ruc=RUC_PRINCIPAL, sol_user="U", sol_password="p")

    with pytest.raises(BizlinksNotConfiguredError):
        BizlinksClient(sin, settings_bizlinks)


# --------------------------------------------------------------------------- #
# Descarga: solo dominios permitidos, sin credenciales
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("url", "esperado"),
    [
        ("https://files.bizlinks.com.pe/x", True),
        ("https://bizlinks.com.pe/x", True),
        ("https://evil-bizlinks.com.pe/x", False),
        ("https://bizlinks.com.pe.evil.com/x", False),
        ("https://169.254.169.254/latest", False),
        ("not-a-url", False),
    ],
)
def test_host_permitido(url: str, esperado: bool) -> None:
    assert host_permitido(url, ["bizlinks.com.pe"]) is esperado


@respx.mock
def test_descargar_no_envia_las_credenciales(cliente: BizlinksClient) -> None:
    ruta = respx.get(URL_XML).mock(return_value=httpx.Response(200, content=b"<xml/>"))

    assert cliente.descargar(URL_XML) == b"<xml/>"
    assert "authorization" not in ruta.calls.last.request.headers


@respx.mock
def test_descargar_rechaza_un_dominio_no_permitido(cliente: BizlinksClient) -> None:
    ruta = respx.get("https://evil.example.com/x").mock(return_value=httpx.Response(200))

    with pytest.raises(BizlinksError):
        cliente.descargar("https://evil.example.com/x")

    assert not ruta.called


@respx.mock
def test_descargar_corta_archivos_enormes(
    cliente: BizlinksClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.sunat.bizlinks.TAMANO_MAXIMO_DESCARGA", 10)
    respx.get(URL_XML).mock(return_value=httpx.Response(200, content=b"x" * 50))

    with pytest.raises(BizlinksError):
        cliente.descargar(URL_XML)


# --------------------------------------------------------------------------- #
# Servicio y API
# --------------------------------------------------------------------------- #
class FakeBizlinksClient:
    """Doble del cliente: documentos por (tipo, serie, correlativo) y archivos por URL."""

    canal = "api"

    def __init__(self) -> None:
        self.documentos: dict[tuple[str, str, int], DocumentoBizlinks] = {}
        self.archivos: dict[str, bytes] = {}
        self.error: Exception | None = None
        self.consultas: list[tuple[str, str, int]] = []

    def obtener_respaldos(
        self,
        *,
        tipo_documento: str,
        serie: str,
        correlativo: int,
        fecha_emision: Any = None,
        monto_total: Any = None,
        necesita_xml: bool = True,
        necesita_cdr: bool = True,
    ) -> RespaldosBizlinks:
        self.consultas.append((tipo_documento, serie, correlativo))
        if self.error is not None:
            raise self.error
        documento = self.documentos.get((tipo_documento, serie, correlativo))
        if documento is None:
            return RespaldosBizlinks(encontrado=False)
        avisos = []
        xml = cdr = None
        if necesita_xml:
            if documento.xml_firmado_url:
                xml = self.archivos[documento.xml_firmado_url]
            else:
                avisos.append("Bizlinks no informó la URL del XML firmado")
        if necesita_cdr:
            if documento.xml_sunat_url:
                cdr = self.archivos[documento.xml_sunat_url]
            else:
                avisos.append("Bizlinks aún no informa el CDR de este comprobante")
        return RespaldosBizlinks(encontrado=True, xml=xml, cdr=cdr, avisos=tuple(avisos))


def _documento(tipo: str = "01", serie: str = "F001", numero: int = 1, **campos: Any):
    base = {
        "tipo_documento": tipo,
        "id_documento": f"{serie}-{numero:08d}",
        "xml_firmado_url": URL_XML,
        "xml_sunat_url": URL_CDR,
    }
    base.update(campos)
    return DocumentoBizlinks(**base)


@pytest.fixture
def biz() -> FakeBizlinksClient:
    fake = FakeBizlinksClient()
    fake.archivos[URL_XML] = (UBL / "factura_con_items.xml").read_bytes()
    fake.archivos[URL_CDR] = empaquetar_cdr_zip(cargar_cdr_xml("cdr_aceptado.xml"))
    return fake


@pytest.fixture
def app_biz(app_r2: FastAPI, biz: FakeBizlinksClient, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    app_r2.state.bizlinks_client_factory = lambda _credenciales, _settings: biz
    return app_r2


@pytest.fixture
def cliente_biz(app_biz: FastAPI):
    with TestClient(app_biz) as c:
        yield c


def _sembrar(uow: FakeUnitOfWork, empresa: Company, **campos: Any):
    comprobante = construir_comprobante(empresa.id, ruc=RUC_PRINCIPAL, **campos)
    uow.comprobantes.comprobantes[comprobante.id] = comprobante
    return comprobante


def _tipos(uow: FakeUnitOfWork, comprobante) -> set[TipoArchivo]:
    return {a.tipo for a in uow.archivos.archivos if a.comprobante_id == comprobante.id}


def test_recupera_xml_y_cdr_y_actualiza_el_estado(
    cliente_biz: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    empresa_principal: Company,
    biz: FakeBizlinksClient,
) -> None:
    comprobante = _sembrar(uow, empresa_principal, correlativo=1)
    biz.documentos[("01", "F001", 1)] = _documento()

    respuesta = cliente_biz.post(RUTA, headers=auth)

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert (cuerpo["completos"], cuerpo["xml_guardados"], cuerpo["cdr_guardados"]) == (1, 1, 1)
    assert cuerpo["items"][0]["items_cargados"] == 2
    assert _tipos(uow, comprobante) == {TipoArchivo.xml, TipoArchivo.cdr}
    assert uow.comprobantes.comprobantes[comprobante.id].estado is EstadoComprobante.aprobado
    assert len(uow.items.items) == 2


def test_el_xml_recuperado_completa_vencimiento_y_forma_de_pago(
    cliente_biz: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    empresa_principal: Company,
    biz: FakeBizlinksClient,
) -> None:
    # El registro de SIRE no trae vencimiento ni forma de pago: los aporta el XML.
    comprobante = _sembrar(uow, empresa_principal, correlativo=1)
    assert comprobante.fecha_vencimiento is None
    assert comprobante.forma_pago is None
    biz.documentos[("01", "F001", 1)] = _documento()
    biz.archivos[URL_XML] = (UBL / "factura_con_pago.xml").read_bytes()

    cliente_biz.post(RUTA, headers=auth)

    actualizado = uow.comprobantes.comprobantes[comprobante.id]
    assert actualizado.fecha_vencimiento == date(2026, 11, 4)
    assert actualizado.forma_pago == "Crédito 30 días"


def test_es_idempotente(
    cliente_biz: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    empresa_principal: Company,
    biz: FakeBizlinksClient,
) -> None:
    _sembrar(uow, empresa_principal, correlativo=1)
    biz.documentos[("01", "F001", 1)] = _documento()
    cliente_biz.post(RUTA, headers=auth)
    biz.consultas.clear()

    segunda = cliente_biz.post(RUTA, headers=auth).json()

    assert segunda["revisados"] == 0
    assert biz.consultas == []


def test_un_xml_de_otro_comprobante_no_se_archiva(
    cliente_biz: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    empresa_principal: Company,
    biz: FakeBizlinksClient,
) -> None:
    # El XML descargado es de F001-1, pero el comprobante local es F001-2.
    comprobante = _sembrar(uow, empresa_principal, correlativo=2)
    biz.documentos[("01", "F001", 2)] = _documento(numero=2, xml_sunat_url=None)

    item = cliente_biz.post(RUTA, headers=auth).json()["items"][0]

    assert item["xml_guardado"] is False
    assert "no corresponde" in item["mensaje"]
    assert TipoArchivo.xml not in _tipos(uow, comprobante)


def test_la_boleta_solo_necesita_el_xml(
    cliente_biz: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    empresa_principal: Company,
    biz: FakeBizlinksClient,
) -> None:
    boleta = _sembrar(uow, empresa_principal, tipo_documento="03", serie="B001", correlativo=5)
    biz.documentos[("03", "B001", 5)] = _documento(
        tipo="03", serie="B001", numero=5, xml_sunat_url=None
    )
    biz.archivos[URL_XML] = (UBL / "boleta.xml").read_bytes()

    item = cliente_biz.post(RUTA, headers=auth).json()["items"][0]

    assert item["resultado"] == "completo"
    assert _tipos(uow, boleta) == {TipoArchivo.xml}


def test_un_cdr_en_xml_suelto_se_archiva_en_zip(
    cliente_biz: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    empresa_principal: Company,
    biz: FakeBizlinksClient,
    almacenamiento,
) -> None:
    comprobante = _sembrar(uow, empresa_principal, correlativo=1)
    biz.documentos[("01", "F001", 1)] = _documento(xml_firmado_url=None)
    biz.archivos[URL_CDR] = cargar_cdr_xml("cdr_aceptado.xml").encode()

    item = cliente_biz.post(RUTA, headers=auth).json()["items"][0]

    assert item["cdr_guardado"] is True
    assert item["resultado"] == "parcial"
    assert _tipos(uow, comprobante) == {TipoArchivo.cdr}
    assert almacenamiento.subidas[-1]["nombre"].endswith("-CDR.zip")


def test_un_comprobante_que_bizlinks_no_tiene_queda_sin_resultado(
    cliente_biz: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    _sembrar(uow, empresa_principal, correlativo=9)

    cuerpo = cliente_biz.post(RUTA, headers=auth).json()

    assert cuerpo["sin_resultado"] == 1
    assert cuerpo["items"][0]["resultado"] == "sin_resultado"


def test_credenciales_rechazadas_detienen_el_lote(
    cliente_biz: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    empresa_principal: Company,
    biz: FakeBizlinksClient,
) -> None:
    for correlativo in (1, 2, 3):
        _sembrar(uow, empresa_principal, correlativo=correlativo)
    biz.error = BizlinksAuthError()

    cuerpo = cliente_biz.post(RUTA, headers=auth).json()

    assert cuerpo["detenido_por"] == "bizlinks_auth_error"
    assert cuerpo["revisados"] == 1
    assert len(biz.consultas) == 1


def test_el_lector_no_puede_ejecutarlo(
    cliente_biz: TestClient, auth_lector: dict[str, str]
) -> None:
    assert cliente_biz.post(RUTA, headers=auth_lector).status_code == 403


def test_sin_credenciales_de_bizlinks_responde_409(
    client: TestClient, auth: dict[str, str]
) -> None:
    """Con la fábrica real y sin BIZLINKS_* en el entorno."""
    respuesta = client.post(RUTA, headers=auth)

    assert respuesta.status_code == 409
    assert respuesta.json()["error"]["code"] == "bizlinks_not_configured"


# --------------------------------------------------------------------------- #
# Enlace con la sincronización de SIRE
# --------------------------------------------------------------------------- #
SYNC_SIRE = f"/api/v1/companies/{RUC_PRINCIPAL}/sire/ventas/sync"


def _propuesta_f001() -> bytes:
    # F001-1 coincide con el XML de `factura_con_items.xml`.
    return construir_zip_propuesta([fila_propuesta(RUC_PRINCIPAL, **{"Serie del CDP": "F001"})])


def test_sire_con_traer_respaldos_completa_xml_y_cdr(
    cliente_biz: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    sire: FakeSireClient,
    biz: FakeBizlinksClient,
) -> None:
    sire.zips = [_propuesta_f001()]
    biz.documentos[("01", "F001", 1)] = _documento()

    respuesta = cliente_biz.post(
        SYNC_SIRE, headers=auth, json={"periodo": "202609", "traer_respaldos": True}
    )

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["nuevos"] == 1
    assert cuerpo["respaldos"]["completos"] == 1
    assert cuerpo["respaldos_error"] is None
    (comprobante,) = uow.comprobantes.comprobantes.values()
    assert _tipos(uow, comprobante) >= {TipoArchivo.xml, TipoArchivo.cdr}
    assert len(uow.items.items) == 2  # el XML completó los ítems del registro de SIRE


def test_sire_sin_el_indicador_no_consulta_a_bizlinks(
    cliente_biz: TestClient,
    auth: dict[str, str],
    sire: FakeSireClient,
    biz: FakeBizlinksClient,
) -> None:
    sire.zips = [_propuesta_f001()]

    cuerpo = cliente_biz.post(SYNC_SIRE, headers=auth, json={"periodo": "202609"}).json()

    assert cuerpo["respaldos"] is None
    assert biz.consultas == []


def test_si_bizlinks_no_esta_configurado_sire_se_completa_igual(
    app_biz: FastAPI, auth: dict[str, str], uow: FakeUnitOfWork, sire: FakeSireClient
) -> None:
    def sin_configurar(_credenciales, _settings):
        raise BizlinksNotConfiguredError()

    app_biz.state.bizlinks_client_factory = sin_configurar
    sire.zips = [_propuesta_f001()]

    with TestClient(app_biz) as c:
        respuesta = c.post(
            SYNC_SIRE, headers=auth, json={"periodo": "202609", "traer_respaldos": True}
        )

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["nuevos"] == 1  # los comprobantes de SIRE no se pierden
    assert cuerpo["respaldos"] is None
    assert "Bizlinks" in cuerpo["respaldos_error"]
    assert len(uow.comprobantes.comprobantes) == 1


def test_si_bizlinks_rechaza_las_credenciales_se_informa_sin_fallar(
    cliente_biz: TestClient,
    auth: dict[str, str],
    sire: FakeSireClient,
    biz: FakeBizlinksClient,
) -> None:
    sire.zips = [_propuesta_f001()]
    biz.error = BizlinksAuthError()

    cuerpo = cliente_biz.post(
        SYNC_SIRE, headers=auth, json={"periodo": "202609", "traer_respaldos": True}
    ).json()

    assert cuerpo["nuevos"] == 1
    assert cuerpo["respaldos"]["detenido_por"] == "bizlinks_auth_error"


def test_si_el_almacenamiento_falla_se_puede_reintentar(
    app_biz: FastAPI,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    empresa_principal: Company,
    biz: FakeBizlinksClient,
) -> None:
    """Bizlinks sí tenía el XML y el CDR: el fallo de R2 es recuperable, no un «no existe»."""
    _sembrar(uow, empresa_principal, correlativo=1)
    biz.documentos[("01", "F001", 1)] = _documento()

    def sin_almacenamiento(_settings):
        raise StorageNotConfiguredError()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.services.store.get_storage_provider", sin_almacenamiento)
        with TestClient(app_biz) as c:
            primera = c.post(RUTA, headers=auth).json()

    assert primera["items"][0]["resultado"] == "error"
    assert primera["items"][0]["error_code"] == "storage_error"
    # No quedó como «sin resultado»: el comprobante se vuelve a intentar de inmediato.
    assert uow.bizlinks_consultas.consultas[0].resultado == "error"
    segunda = TestClient(app_biz).post(RUTA, headers=auth).json()
    assert segunda["revisados"] == 1
