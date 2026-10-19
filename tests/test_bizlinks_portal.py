"""Portal anónimo de Bizlinks: flujo JSF, descargas y reglas de convivencia.

La página nunca se toca de verdad: se simulan las respuestas parciales de
PrimeFaces con la misma forma que devuelve el servidor. El comprobante usado
(FF04-600, real) se reemplaza por el XML de prueba.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.core.config import Settings
from app.core.errors import (
    BizlinksBlockedError,
    BizlinksError,
    BizlinksPortalChangedError,
)
from app.secrets.base import CompanyCredentials
from app.sunat.bizlinks_portal import BizlinksPortalClient
from tests.conftest import RUC_PRINCIPAL

UBL = Path(__file__).parent / "fixtures" / "ubl"
URL = "https://sfe.bizlinks.com.pe/sfeperu/public/loginAnonimo.jsf"
CDR_XML = (
    '<ApplicationResponse xmlns="urn:oasis:names:specification:ubl:schema:xsd:'
    'ApplicationResponse-2"><cbc:ID xmlns:cbc="urn:oasis:names:specification:'
    'ubl:schema:xsd:CommonBasicComponents-2">0</cbc:ID></ApplicationResponse>'
)


def _zip(contenido: bytes, nombre: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archivo:
        archivo.writestr(nombre, contenido)
    return buffer.getvalue()


FORMULARIO = """
<html><body><form id="loginForm">
  <input type="hidden" name="javax.faces.ViewState" value="VS-1"/>
  <select name="Documento_input"><option value="01">Factura</option></select>
  <input name="nuSerieNumero"/>
  <input name="montoImporteTotal"/>
  <input name="fechaEmision_input"/>
  <input name="nuTributarioEmisor"/>
  <button type="submit" id="loginForm:btnConsultar"
          onclick="PrimeFaces.ab({formId:'loginForm',source:'loginForm:btnConsultar'});"/>
</form></body></html>
"""


def _parcial(contenido: str, viewstate: str = "VS-2") -> str:
    """Respuesta parcial de JSF: ViewState + trozos de la vista."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<partial-response><changes>'
        f'<update id="javax.faces.ViewState"><![CDATA[{viewstate}]]></update>'
        f"{contenido}"
        "</changes></partial-response>"
    )


def _detalle_abierto() -> str:
    return _parcial("<eval>PrimeFaces.ab({});dlgVerDetalle.show();</eval>")


def _error(mensaje: str) -> str:
    return _parcial(
        '<update id="dlgMessageErrors"><![CDATA['
        f'<div class="ui-dialog-content">{mensaje}</div><button>Aceptar</button>'
        "]]></update><eval>dlgMessageErrors.show();</eval>"
    )


@pytest.fixture
def settings_portal() -> Settings:
    return Settings(
        _env_file=None,
        environment="local",
        bizlinks_portal_url=URL,
        bizlinks_portal_min_interval_seconds=0,
        bizlinks_portal_max_interval_seconds=0,
    )


@pytest.fixture
def credenciales() -> CompanyCredentials:
    return CompanyCredentials(ruc=RUC_PRINCIPAL, sol_user="MODDATOS", sol_password="clave-sol")


def _cliente(credenciales: CompanyCredentials, settings: Settings) -> BizlinksPortalClient:
    return BizlinksPortalClient(credenciales, settings, sleep=lambda _s: None)


def _consultar(cliente: BizlinksPortalClient, **cambios: Any):
    datos = {
        "tipo_documento": "01",
        "serie": "FF04",
        "correlativo": 600,
        "fecha_emision": date(2026, 9, 30),
        "monto_total": Decimal("25279.93"),
    }
    datos.update(cambios)
    return cliente.obtener_respaldos(**datos)


@respx.mock
def test_descarga_el_xml_y_el_cdr_del_portal(
    credenciales: CompanyCredentials, settings_portal: Settings
) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, text=FORMULARIO))
    respx.post(URL).mock(
        side_effect=[
            httpx.Response(200, text=_detalle_abierto()),
            httpx.Response(
                200,
                text=_parcial(
                    '<extension id="x" type="args"><![CDATA[{"seleccionado":true}]]></extension>'
                ),
            ),
            httpx.Response(
                200,
                content=_zip((UBL / "factura.xml").read_bytes(), "FF04-600.xml"),
                headers={"Content-Type": "application/zip"},
            ),
            httpx.Response(
                200,
                text=_parcial(
                    '<extension id="x" type="args"><![CDATA[{"seleccionado":true}]]></extension>',
                    "VS-4",
                ),
            ),
            httpx.Response(
                200, content=_zip(CDR_XML.encode(), "R-01-FF04-600.xml")
            ),
        ]
    )

    respaldos = _consultar(_cliente(credenciales, settings_portal))

    assert respaldos.encontrado is True
    assert respaldos.xml == (UBL / "factura.xml").read_bytes()
    assert respaldos.cdr is not None and respaldos.cdr.startswith(b"PK")
    assert respaldos.avisos == ()


@respx.mock
def test_la_peticion_lleva_los_cinco_datos_y_el_viewstate(
    credenciales: CompanyCredentials, settings_portal: Settings
) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, text=FORMULARIO))
    consulta = respx.post(URL).mock(return_value=httpx.Response(200, text=_detalle_abierto()))

    _consultar(_cliente(credenciales, settings_portal), necesita_xml=False, necesita_cdr=False)

    cuerpo = consulta.calls.last.request.content.decode()
    assert "javax.faces.ViewState=VS-1" in cuerpo
    assert "nuSerieNumero=FF04-00000600" in cuerpo
    assert "montoImporteTotal=25279.93" in cuerpo
    assert "fechaEmision_input=30%2F09%2F2026" in cuerpo
    assert f"nuTributarioEmisor={RUC_PRINCIPAL}" in cuerpo


@respx.mock
def test_sin_fecha_o_monto_no_se_consulta(
    credenciales: CompanyCredentials, settings_portal: Settings
) -> None:
    ruta = respx.get(URL).mock(return_value=httpx.Response(200, text=FORMULARIO))

    respaldos = _consultar(_cliente(credenciales, settings_portal), fecha_emision=None)

    assert respaldos.encontrado is False
    assert "fecha de emisión" in respaldos.avisos[0]
    assert not ruta.called


@respx.mock
def test_un_documento_inexistente_devuelve_el_mensaje_del_portal(
    credenciales: CompanyCredentials, settings_portal: Settings
) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, text=FORMULARIO))
    respx.post(URL).mock(
        return_value=httpx.Response(
            200, text=_error("Los datos ingresados no corresponden a ningun documento.")
        )
    )

    respaldos = _consultar(_cliente(credenciales, settings_portal))

    assert respaldos.encontrado is False
    assert "no corresponden" in (respaldos.avisos[0] or "")


@respx.mock
@pytest.mark.parametrize("estado", [403, 429])
def test_un_bloqueo_no_se_intenta_saltar(
    credenciales: CompanyCredentials, settings_portal: Settings, estado: int
) -> None:
    respx.get(URL).mock(return_value=httpx.Response(estado, text="bloqueado"))

    with pytest.raises(BizlinksBlockedError) as error:
        _consultar(_cliente(credenciales, settings_portal))

    assert error.value.details["http_status"] == estado


@respx.mock
def test_un_captcha_detiene_las_consultas(
    credenciales: CompanyCredentials, settings_portal: Settings
) -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(200, text=FORMULARIO.replace("<body>", "<body>captcha"))
    )

    with pytest.raises(BizlinksBlockedError):
        _consultar(_cliente(credenciales, settings_portal))


@respx.mock
def test_si_la_pagina_cambia_no_se_adivina(
    credenciales: CompanyCredentials, settings_portal: Settings
) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html><body>otra cosa</body>"))

    with pytest.raises(BizlinksPortalChangedError):
        _consultar(_cliente(credenciales, settings_portal))


@respx.mock
def test_si_la_consulta_no_abre_el_detalle_ni_da_error_se_avisa(
    credenciales: CompanyCredentials, settings_portal: Settings
) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, text=FORMULARIO))
    respx.post(URL).mock(return_value=httpx.Response(200, text=_parcial("")))

    with pytest.raises(BizlinksPortalChangedError):
        _consultar(_cliente(credenciales, settings_portal))


def test_una_url_de_portal_ajena_no_se_usa(
    credenciales: CompanyCredentials, settings_portal: Settings
) -> None:
    ajeno = settings_portal.model_copy(update={"bizlinks_portal_url": "https://evil.example.com/x"})

    with pytest.raises(BizlinksError):
        BizlinksPortalClient(credenciales, ajeno)


@respx.mock
def test_si_un_archivo_no_esta_seleccionado_se_informa(
    credenciales: CompanyCredentials, settings_portal: Settings
) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, text=FORMULARIO))
    respx.post(URL).mock(
        side_effect=[
            httpx.Response(200, text=_detalle_abierto()),
            httpx.Response(200, text=_parcial("")),  # el botón no seleccionó nada
        ]
    )

    respaldos = _consultar(
        _cliente(credenciales, settings_portal), necesita_cdr=False, necesita_xml=True
    )

    assert respaldos.encontrado is True
    assert respaldos.xml is None
    assert "XML" in (respaldos.avisos[0] or "")


def test_el_mensaje_de_error_sale_sin_html_ni_javascript() -> None:
    from app.sunat.bizlinks_portal import _mensaje_de_error

    parcial = _parcial(
        '<update id="dlgMessageErrors"><![CDATA['
        "<!-- $(function() {PrimeFaces.cw('Dialog','dlgMessageErrors',{});}); //-->"
        '<div class="ui-dialog-content">Información Los datos ingresados no corresponden '
        "a ningun documento ingresado.</div><button>Aceptar</button>"
        "]]></update>"
    ).encode()

    assert _mensaje_de_error(parcial) == (
        "Los datos ingresados no corresponden a ningun documento ingresado."
    )
