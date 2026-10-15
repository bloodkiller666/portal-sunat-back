"""Pruebas de la fecha de vencimiento y la forma de pago del XML UBL."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.sunat.ubl import parse_comprobante_ubl

UBL = Path(__file__).parent / "fixtures" / "ubl"

BASE = """<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>F001-00000009</cbc:ID>
  <cbc:IssueDate>2026-10-05</cbc:IssueDate>
  {cabecera}
  <cac:AccountingSupplierParty>
    <cac:Party>
      <cac:PartyIdentification><cbc:ID schemeID="6">20123456789</cbc:ID></cac:PartyIdentification>
    </cac:Party>
  </cac:AccountingSupplierParty>
  {pago}
</Invoice>"""


def _parse(pago: str = "", cabecera: str = ""):
    xml = BASE.replace("{pago}", pago).replace("{cabecera}", cabecera)
    return parse_comprobante_ubl(xml.encode())


def test_credito_en_cuotas_vence_con_la_ultima_cuota() -> None:
    dato = parse_comprobante_ubl((UBL / "factura_credito.xml").read_bytes())

    assert dato.fecha_vencimiento == date(2026, 11, 4)
    assert dato.forma_pago == "Crédito 30 días"


def test_contado_no_lleva_dias() -> None:
    dato = _parse(
        "<cac:PaymentTerms><cbc:ID>FormaPago</cbc:ID>"
        "<cbc:PaymentMeansID>Contado</cbc:PaymentMeansID></cac:PaymentTerms>"
    )

    assert dato.forma_pago == "Contado"
    assert dato.fecha_vencimiento is None


def test_due_date_de_cabecera_tiene_prioridad() -> None:
    dato = _parse(
        "<cac:PaymentTerms><cbc:PaymentMeansID>Credito</cbc:PaymentMeansID>"
        "<cbc:PaymentDueDate>2026-12-31</cbc:PaymentDueDate></cac:PaymentTerms>",
        cabecera="<cbc:DueDate>2026-10-25</cbc:DueDate>",
    )

    assert dato.fecha_vencimiento == date(2026, 10, 25)
    assert dato.forma_pago == "Crédito 20 días"


def test_payment_con_fecha_de_vencimiento() -> None:
    dato = _parse("<cac:Payment><cbc:PaymentDueDate>2026-10-15</cbc:PaymentDueDate></cac:Payment>")

    assert dato.fecha_vencimiento == date(2026, 10, 15)
    assert dato.forma_pago is None


def test_credito_sin_fecha_es_solo_credito_y_acepta_tilde_en_el_id() -> None:
    dato = _parse("<cac:PaymentTerms><cbc:ID>Crédito</cbc:ID></cac:PaymentTerms>")

    assert dato.forma_pago == "Crédito"


@pytest.mark.parametrize("fixture", ["factura.xml", "boleta.xml", "nota_credito.xml"])
def test_sin_condiciones_de_pago_es_tolerante(fixture: str) -> None:
    dato = parse_comprobante_ubl((UBL / fixture).read_bytes())

    assert dato.fecha_vencimiento is None
    assert dato.forma_pago is None


def test_fecha_de_vencimiento_invalida_se_ignora() -> None:
    dato = _parse(cabecera="<cbc:DueDate>no-es-fecha</cbc:DueDate>")

    assert dato.fecha_vencimiento is None
