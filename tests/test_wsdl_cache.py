"""El WSDL real de SUNAT importa otro documento: la caché debe resolverlo en disco."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from app.core.config import Settings
from app.secrets.base import CompanyCredentials
from app.sunat.consult import SunatConsultClient
from app.sunat.endpoints import ServicioSunat
from app.sunat.wsdl import WsdlLoader
from tests.fakes import ENDPOINT_CONSULTA, cdr_base64

BASE = "https://sunat.test/billConsultService?wsdl"
INNER = "https://sunat.test/billConsultService?wsdl=1"

WSDL_BASE = f"""<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://schemas.xmlsoap.org/wsdl/"
    xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/"
    xmlns:tns="http://service.ws.consulta.sunat.gob.pe/"
    xmlns:ns1="http://service.sunat.gob.pe"
    targetNamespace="http://service.ws.consulta.sunat.gob.pe/" name="billConsultService">
  <import namespace="http://service.sunat.gob.pe" location="{INNER}"/>
  <binding name="BillConsultServicePortBinding" type="ns1:billService">
    <soap:binding transport="http://schemas.xmlsoap.org/soap/http" style="document"/>
    <operation name="getStatusCdr">
      <soap:operation soapAction="urn:getStatusCdr"/>
      <input><soap:body use="literal"/></input>
      <output><soap:body use="literal"/></output>
    </operation>
  </binding>
  <service name="billConsultService">
    <port name="BillConsultServicePort" binding="tns:BillConsultServicePortBinding">
      <soap:address location="{ENDPOINT_CONSULTA}"/>
    </port>
  </service>
</definitions>"""

WSDL_INNER = """<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://schemas.xmlsoap.org/wsdl/"
    xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/"
    xmlns:xsd="http://www.w3.org/2001/XMLSchema"
    xmlns:tns="http://service.sunat.gob.pe"
    targetNamespace="http://service.sunat.gob.pe" name="billService">
  <types>
    <xsd:schema targetNamespace="http://service.sunat.gob.pe" elementFormDefault="qualified">
      <xsd:complexType name="statusCdrResponseType">
        <xsd:sequence>
          <xsd:element name="statusCode" type="xsd:string" minOccurs="0"/>
          <xsd:element name="statusMessage" type="xsd:string" minOccurs="0"/>
          <xsd:element name="content" type="xsd:base64Binary" minOccurs="0"/>
        </xsd:sequence>
      </xsd:complexType>
      <xsd:element name="getStatusCdr">
        <xsd:complexType>
          <xsd:sequence>
            <xsd:element name="rucComprobante" type="xsd:string"/>
            <xsd:element name="tipoComprobante" type="xsd:string"/>
            <xsd:element name="serieComprobante" type="xsd:string"/>
            <xsd:element name="numeroComprobante" type="xsd:string"/>
            <xsd:element name="montoTotal" type="xsd:decimal" minOccurs="0"/>
          </xsd:sequence>
        </xsd:complexType>
      </xsd:element>
      <xsd:element name="getStatusCdrResponse">
        <xsd:complexType>
          <xsd:sequence>
            <xsd:element name="statusCdr" type="tns:statusCdrResponseType"/>
          </xsd:sequence>
        </xsd:complexType>
      </xsd:element>
    </xsd:schema>
  </types>
  <message name="getStatusCdrRequest">
    <part name="parameters" element="tns:getStatusCdr"/>
  </message>
  <message name="getStatusCdrResponse">
    <part name="parameters" element="tns:getStatusCdrResponse"/>
  </message>
  <portType name="billService">
    <operation name="getStatusCdr">
      <input message="tns:getStatusCdrRequest"/>
      <output message="tns:getStatusCdrResponse"/>
    </operation>
  </portType>
</definitions>"""


def _cliente_mock(estado: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if estado != 200:
            return httpx.Response(estado, text="error")
        if str(request.url) == BASE:
            return httpx.Response(200, text=WSDL_BASE)
        if str(request.url) == INNER:
            return httpx.Response(200, text=WSDL_INNER)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "sunat_env": "prod",
        "sunat_consult_service_url": ENDPOINT_CONSULTA,
        "sunat_wsdl_dir": tmp_path,
        "sunat_max_retries": 0,
    }
    base.update(overrides)
    return Settings(**base)


def test_guarda_el_wsdl_y_sus_imports_en_disco(tmp_path: Path) -> None:
    loader = WsdlLoader(_settings(tmp_path), client=_cliente_mock())

    ruta = loader.resolve(ServicioSunat.consult, BASE)

    assert Path(ruta).name == "billConsultService.wsdl"
    contenido = Path(ruta).read_text(encoding="utf-8")
    # El import absoluto se reescribe al archivo local.
    assert INNER not in contenido
    assert 'location="billConsultService-' in contenido

    importados = [p for p in tmp_path.iterdir() if p.name != "billConsultService.wsdl"]
    assert len(importados) == 1
    assert "statusCdrResponseType" in importados[0].read_text(encoding="utf-8")


@respx.mock
def test_el_wsdl_cacheado_se_usa_sin_volver_a_descargar(tmp_path: Path) -> None:
    loader = WsdlLoader(_settings(tmp_path), client=_cliente_mock())
    loader.resolve(ServicioSunat.consult, BASE)

    # Solo se mockea el POST del SOAP: si zeep intentara descargar algo, fallaría.
    respx.post(ENDPOINT_CONSULTA).mock(
        return_value=httpx.Response(
            200,
            text=(
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
                "<soapenv:Body>"
                '<ns:getStatusCdrResponse xmlns:ns="http://service.sunat.gob.pe">'
                "<ns:statusCdr><ns:statusCode>0</ns:statusCode>"
                f"<ns:content>{cdr_base64()}</ns:content>"
                "</ns:statusCdr></ns:getStatusCdrResponse>"
                "</soapenv:Body></soapenv:Envelope>"
            ),
        )
    )
    client = SunatConsultClient(
        CompanyCredentials(ruc="20123456789", sol_user="MODDATOS", sol_password="clave"),
        _settings(tmp_path),
    )

    resultado = client.get_status_cdr(tipo_comprobante="01", serie="F001", numero="00000001")

    assert resultado.estado.codigo == "0"
    assert resultado.cdr is not None
    # El puerto del WSDL real se descubre solo y el endpoint configurado manda.
    assert resultado.tramas.endpoint == ENDPOINT_CONSULTA


def test_si_falla_la_descarga_se_devuelve_la_url(tmp_path: Path) -> None:
    loader = WsdlLoader(_settings(tmp_path), client=_cliente_mock(estado=500))

    assert loader.resolve(ServicioSunat.consult, BASE) == BASE
    assert not list(tmp_path.iterdir())


def test_sin_directorio_configurado_no_se_descarga_nada(tmp_path: Path) -> None:
    loader = WsdlLoader(_settings(tmp_path, sunat_wsdl_dir=""), client=_cliente_mock())

    assert loader.resolve(ServicioSunat.consult, BASE) == BASE
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("wsdl_url", [BASE, INNER])
def test_nombres_locales_estables(wsdl_url: str) -> None:
    from app.sunat.wsdl import _nombre_local

    nombre = _nombre_local(wsdl_url)
    assert nombre.endswith(".wsdl")
    assert nombre == _nombre_local(wsdl_url)
    assert nombre.split("-")[0] == "billConsultService"
