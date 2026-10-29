from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.core.errors import InvalidDocumentError
from app.sunat.ubl import parse_comprobante_ubl

UBL = Path(__file__).parent / "fixtures" / "ubl"


def _leer(nombre: str) -> bytes:
    return (UBL / nombre).read_bytes()


def test_factura_extrae_los_campos() -> None:
    dato = parse_comprobante_ubl(_leer("factura.xml"))

    assert dato.tipo_documento == "01"
    assert dato.serie == "F001"
    assert dato.correlativo == 1
    assert dato.ruc_emisor == "20123456789"
    assert dato.fecha_emision.isoformat() == "2026-10-05"
    assert dato.moneda == "PEN"
    assert dato.monto_total == Decimal("118.00")
    assert dato.nombre_archivo == "20123456789-01-F001-00000001"


def test_boleta_se_detecta_por_la_serie() -> None:
    dato = parse_comprobante_ubl(_leer("boleta.xml"))
    assert dato.tipo_documento == "03"
    assert dato.correlativo == 5
    assert dato.moneda == "USD"


def test_nota_de_credito() -> None:
    dato = parse_comprobante_ubl(_leer("nota_credito.xml"))
    assert dato.tipo_documento == "07"
    assert dato.serie == "FC01"
    assert dato.correlativo == 10


def test_ruc_esperado_distinto_se_rechaza() -> None:
    with pytest.raises(InvalidDocumentError, match="otro RUC"):
        parse_comprobante_ubl(_leer("factura.xml"), ruc_esperado="20999999999")


def test_xml_invalido() -> None:
    with pytest.raises(InvalidDocumentError, match="XML válido"):
        parse_comprobante_ubl(b"<Invoice")


def test_documento_no_ubl() -> None:
    with pytest.raises(InvalidDocumentError, match="comprobante UBL"):
        parse_comprobante_ubl(b"<html><body>no</body></html>")


def test_identificador_invalido() -> None:
    xml = _leer("factura.xml").replace(b"F001-00000001", b"F001")
    with pytest.raises(InvalidDocumentError, match="identificador"):
        parse_comprobante_ubl(xml)


def test_serie_desconocida_no_define_tipo() -> None:
    xml = _leer("factura.xml").replace(b"F001-00000001", b"X001-00000001")
    with pytest.raises(InvalidDocumentError, match="tipo de comprobante"):
        parse_comprobante_ubl(xml)


def test_una_serie_e001_se_toma_del_codigo_de_tipo_del_xml() -> None:
    # Factura electrónica emitida desde SOL: la serie empieza por E, pero el XML dice que es 01.
    xml = (
        _leer("factura.xml")
        .replace(b"F001-00000001", b"E001-00000010")
        .replace(
            b"<cbc:ID>E001-00000010</cbc:ID>",
            b"<cbc:ID>E001-00000010</cbc:ID><cbc:InvoiceTypeCode>01</cbc:InvoiceTypeCode>",
        )
    )

    dato = parse_comprobante_ubl(xml)

    assert (dato.tipo_documento, dato.serie, dato.correlativo) == ("01", "E001", 10)


def test_un_codigo_de_tipo_no_reconocido_vuelve_a_la_serie() -> None:
    xml = _leer("factura.xml").replace(
        b"<cbc:ID>F001-00000001</cbc:ID>",
        b"<cbc:ID>F001-00000001</cbc:ID><cbc:InvoiceTypeCode>99</cbc:InvoiceTypeCode>",
    )

    assert parse_comprobante_ubl(xml).tipo_documento == "01"


def test_xml_facele_se_lee_como_un_comprobante_normal() -> None:
    """El esquema `facele.pe` (`Comprobante/cpe`) de algunos proveedores también se importa."""
    dato = parse_comprobante_ubl(_leer("factura_facele.xml"))

    assert (dato.tipo_documento, dato.serie, dato.correlativo) == ("01", "FL50", 4650)
    assert dato.nombre_archivo == "20123456786-01-FL50-00004650"
    assert dato.ruc_emisor == "20123456786"
    assert dato.ruc_receptor == "20514831620"
    assert dato.fecha_emision.isoformat() == "2026-10-28"
    assert dato.fecha_vencimiento.isoformat() == "2026-11-27"
    assert dato.forma_pago == "Crédito 30 días"
    assert dato.moneda == "PEN"
    assert dato.monto_total == Decimal("459.02")
    assert [(i.cantidad, i.precio_unitario, i.igv) for i in dato.items] == [
        (Decimal("3.0000"), Decimal("50.7400"), Decimal("23.22")),
        (Decimal("4.0000"), Decimal("76.7000"), Decimal("46.80")),
    ]


def test_xml_facele_de_otro_ruc_se_rechaza_si_se_pide_ruc() -> None:
    with pytest.raises(InvalidDocumentError):
        parse_comprobante_ubl(_leer("factura_facele.xml"), ruc_esperado="20999999999")
