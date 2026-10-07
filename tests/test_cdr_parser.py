from __future__ import annotations

import base64
import io
import zipfile
from datetime import datetime

import pytest

from app.core.errors import CdrParseError
from app.sunat.cdr import parse_cdr_xml, parse_cdr_zip
from tests.fakes import cargar_cdr_xml, empaquetar_cdr_zip


def test_parsea_cdr_aceptado_con_notas() -> None:
    info = parse_cdr_xml(cargar_cdr_xml("cdr_aceptado.xml"))
    assert info.codigo == "0"
    assert info.aceptado is True
    assert "ha sido aceptada" in info.descripcion
    assert info.notas == [
        "El comprobante fue aceptado con observaciones",
        "Se sugiere revisar el dato del receptor",
    ]
    assert info.identificador == "20123456789-01-F001-1"
    assert info.fecha_respuesta == datetime(2026, 1, 15, 10, 30, 5)


def test_parsea_cdr_rechazado() -> None:
    info = parse_cdr_xml(cargar_cdr_xml("cdr_rechazado.xml"))
    assert info.codigo == "2025"
    assert info.aceptado is False
    assert "rechazado" in info.descripcion
    assert info.notas == []


def test_parsea_el_zip_en_base64() -> None:
    xml = cargar_cdr_xml("cdr_aceptado.xml")
    info = parse_cdr_zip(base64.b64encode(empaquetar_cdr_zip(xml)).decode())
    assert info.codigo == "0"
    assert info.archivo == "R-20123456789-01-F001-1.xml"
    assert info.xml is not None and "ApplicationResponse" in info.xml


def test_parsea_el_zip_crudo_que_devuelve_zeep() -> None:
    # zeep convierte xsd:base64Binary a bytes: hay que soportar el ZIP directo.
    info = parse_cdr_zip(empaquetar_cdr_zip(cargar_cdr_xml("cdr_aceptado.xml")))
    assert info.codigo == "0"


def test_tolera_prefijos_y_ausencia_de_namespace() -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <ApplicationResponse>
      <ID>20123456789-01-F001-9</ID>
      <DocumentResponse>
        <Response>
          <ResponseCode>0</ResponseCode>
          <Description>Aceptada</Description>
        </Response>
      </DocumentResponse>
    </ApplicationResponse>"""
    info = parse_cdr_xml(xml)
    assert info.codigo == "0"
    assert info.descripcion == "Aceptada"


def test_base64_invalido() -> None:
    with pytest.raises(CdrParseError, match="base64"):
        parse_cdr_zip("no-es-un-zip-ni-base64")


def test_contenido_vacio() -> None:
    with pytest.raises(CdrParseError, match="vacío"):
        parse_cdr_zip("   ")


def test_zip_sin_xml() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archivo:
        archivo.writestr("leeme.txt", "sin xml")
    with pytest.raises(CdrParseError, match="ningún archivo XML"):
        parse_cdr_zip(buffer.getvalue())


def test_xml_sin_codigo_de_respuesta() -> None:
    with pytest.raises(CdrParseError, match="ResponseCode"):
        parse_cdr_xml("<ApplicationResponse><ID>1</ID></ApplicationResponse>")


def test_xml_mal_formado() -> None:
    with pytest.raises(CdrParseError, match="mal formado"):
        parse_cdr_xml("<ApplicationResponse><ID>")
