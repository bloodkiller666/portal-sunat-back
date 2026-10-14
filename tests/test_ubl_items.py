"""Pruebas del parser de ítems del XML UBL (`app.sunat.ubl`)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.sunat.ubl import parse_comprobante_ubl

UBL = Path(__file__).parent / "fixtures" / "ubl"

FACTURA_BASE = """<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>F001-00000009</cbc:ID>
  <cbc:IssueDate>2026-10-05</cbc:IssueDate>
  <cbc:DocumentCurrencyCode>PEN</cbc:DocumentCurrencyCode>
  <cac:AccountingSupplierParty>
    <cac:Party>
      <cac:PartyIdentification><cbc:ID schemeID="6">20123456789</cbc:ID></cac:PartyIdentification>
    </cac:Party>
  </cac:AccountingSupplierParty>
  <cac:LegalMonetaryTotal>
    <cbc:PayableAmount currencyID="PEN">118.00</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
  {lineas}
</Invoice>"""


def _xml(nombre: str) -> bytes:
    return (UBL / nombre).read_bytes()


def _factura(lineas: str) -> bytes:
    return FACTURA_BASE.replace("{lineas}", lineas).encode()


def test_factura_con_items_extrae_las_lineas() -> None:
    dato = parse_comprobante_ubl(_xml("factura_con_items.xml"))

    assert len(dato.items) == 2

    primero = dato.items[0]
    assert primero.item_nro == 1
    assert primero.codigo == "PROD-01"
    assert primero.descripcion == "Cemento Portland Tipo I"
    assert primero.cantidad == Decimal("2.00")
    assert primero.unidad_medida == "NIU"
    # Valor unitario sin IGV: valor de venta / cantidad.
    assert primero.valor_unitario == Decimal("50.00")
    assert primero.precio_unitario == Decimal("59.00")  # con IGV (Price/BaseQuantity)
    assert primero.descuento == Decimal("5.00")
    assert primero.igv == Decimal("18.00")
    assert primero.isc == Decimal("0")
    assert primero.tip_afectacion_igv == "10"
    assert primero.total == Decimal("100.00")

    segundo = dato.items[1]
    assert segundo.item_nro == 2
    assert segundo.codigo is None
    assert segundo.descripcion == "Asfalto liquidito a granel"
    assert segundo.cantidad == Decimal("3.00")
    assert segundo.unidad_medida == "KGM"
    assert segundo.valor_unitario == Decimal("20.00")
    # Precio repartido en la BaseQuantity del XML (47.20 / 2).
    assert segundo.precio_unitario == Decimal("23.60")
    assert segundo.igv == Decimal("10.80")
    assert segundo.total == Decimal("60.00")


def test_nota_de_credito_lee_sus_lineas() -> None:
    dato = parse_comprobante_ubl(_xml("nota_credito_con_items.xml"))

    assert dato.tipo_documento == "07"
    assert len(dato.items) == 1
    linea = dato.items[0]
    assert linea.descripcion == "Devolución de Cemento Portland Tipo I"
    assert linea.cantidad == Decimal("1.00")
    assert linea.unidad_medida == "NIU"
    assert linea.total == Decimal("-50.00")
    assert linea.igv == Decimal("-9.00")
    assert linea.precio_unitario == Decimal("-59.00")


def test_xml_sin_lineas_devuelve_items_vacios() -> None:
    dato = parse_comprobante_ubl(_xml("factura.xml"))
    assert dato.items == ()


def test_linea_con_cantidad_invalida_se_omite() -> None:
    xml = _factura(
        """  <cac:InvoiceLine>
          <cbc:ID>1</cbc:ID>
          <cbc:InvoicedQuantity unitCode="NIU">0.00</cbc:InvoicedQuantity>
          <cbc:LineExtensionAmount currencyID="PEN">10.00</cbc:LineExtensionAmount>
          <cac:Item><cbc:Description>Mala línea</cbc:Description></cac:Item>
        </cac:InvoiceLine>
        <cac:InvoiceLine>
          <cbc:ID>2</cbc:ID>
          <cbc:InvoicedQuantity unitCode="NIU">1.00</cbc:InvoicedQuantity>
          <cbc:LineExtensionAmount currencyID="PEN">10.00</cbc:LineExtensionAmount>
          <cac:Item><cbc:Description>Buena línea</cbc:Description></cac:Item>
        </cac:InvoiceLine>"""
    )

    dato = parse_comprobante_ubl(xml)

    assert len(dato.items) == 1
    assert dato.items[0].descripcion == "Buena línea"


def test_linea_sin_cantidad_se_asume_unitaria() -> None:
    xml = _factura(
        """  <cac:InvoiceLine>
          <cbc:ID>1</cbc:ID>
          <cbc:LineExtensionAmount currencyID="PEN">10.00</cbc:LineExtensionAmount>
          <cac:Item><cbc:Description>Servicio único</cbc:Description></cac:Item>
        </cac:InvoiceLine>"""
    )

    dato = parse_comprobante_ubl(xml)

    assert len(dato.items) == 1
    assert dato.items[0].cantidad == Decimal("1")
    assert dato.items[0].valor_unitario == Decimal("10.00")


def test_linea_sin_descripcion_usa_el_codigo_o_un_texto_por_defecto() -> None:
    xml = _factura(
        """  <cac:InvoiceLine>
          <cbc:ID>1</cbc:ID>
          <cbc:InvoicedQuantity unitCode="NIU">1.00</cbc:InvoicedQuantity>
          <cbc:LineExtensionAmount currencyID="PEN">10.00</cbc:LineExtensionAmount>
          <cac:Item>
            <cac:SellersItemIdentification><cbc:ID>SKU-99</cbc:ID></cac:SellersItemIdentification>
          </cac:Item>
        </cac:InvoiceLine>
        <cac:InvoiceLine>
          <cbc:ID>2</cbc:ID>
          <cbc:InvoicedQuantity unitCode="NIU">1.00</cbc:InvoicedQuantity>
          <cbc:LineExtensionAmount currencyID="PEN">20.00</cbc:LineExtensionAmount>
          <cac:Item />
        </cac:InvoiceLine>"""
    )

    dato = parse_comprobante_ubl(xml)

    assert dato.items[0].descripcion == "SKU-99"
    assert dato.items[1].descripcion == "Ítem 2"


def test_linea_vacia_se_omite() -> None:
    xml = _factura(
        """  <cac:InvoiceLine>
          <cbc:ID>1</cbc:ID>
        </cac:InvoiceLine>"""
    )

    dato = parse_comprobante_ubl(xml)

    assert dato.items == ()


def test_los_impuestos_ajenos_al_igv_no_se_confunden() -> None:
    """ISC (2000) se desglosa aparte; un esquema desconocido no se cuenta como IGV."""
    xml = _factura(
        """  <cac:InvoiceLine>
          <cbc:ID>1</cbc:ID>
          <cbc:InvoicedQuantity unitCode="NIU">1.00</cbc:InvoicedQuantity>
          <cbc:LineExtensionAmount currencyID="PEN">100.00</cbc:LineExtensionAmount>
          <cac:TaxTotal>
            <cbc:TaxAmount currencyID="PEN">23.00</cbc:TaxAmount>
            <cac:TaxSubtotal>
              <cbc:TaxAmount currencyID="PEN">3.00</cbc:TaxAmount>
              <cac:TaxCategory>
                <cbc:ID>17</cbc:ID>
                <cac:TaxScheme><cbc:ID>2000</cbc:ID><cbc:Name>ISC</cbc:Name></cac:TaxScheme>
              </cac:TaxCategory>
            </cac:TaxSubtotal>
            <cac:TaxSubtotal>
              <cbc:TaxAmount currencyID="PEN">20.00</cbc:TaxAmount>
              <cac:TaxCategory>
                <cbc:ID>10</cbc:ID>
                <cac:TaxScheme><cbc:ID>1000</cbc:ID><cbc:Name>IGV</cbc:Name></cac:TaxScheme>
              </cac:TaxCategory>
            </cac:TaxSubtotal>
          </cac:TaxTotal>
          <cac:Item><cbc:Description>Producto con ISC</cbc:Description></cac:Item>
        </cac:InvoiceLine>"""
    )

    dato = parse_comprobante_ubl(xml)

    linea = dato.items[0]
    assert linea.igv == Decimal("20.00")
    assert linea.isc == Decimal("3.00")
    assert linea.tip_afectacion_igv == "10"
