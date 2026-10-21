"""Corrección de montos en USD de las ventas ya cargadas: el plan (puro, sin base ni R2)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from scripts.corregir_montos_sire import planificar_correcciones

from app.sunat.sire_rvie import RegistroSire, monto_en_moneda


def _registro(correlativo: int, total: str, tc: str | None, moneda: str = "USD") -> RegistroSire:
    cambio = Decimal(tc) if tc else None
    monto, monto_pen = monto_en_moneda(Decimal(total), moneda, cambio)
    return RegistroSire(
        ruc_emisor="20514831620",
        razon_social_emisor="ACME",
        tipo_documento="01",
        serie="FF02",
        correlativo=correlativo,
        fecha_emision=date(2026, 8, 4),
        fecha_vencimiento=None,
        moneda=moneda,
        monto_total=monto,
        anulado=False,
        receptor_tipo_doc="0",
        receptor_doc="300972021",
        razon_social_receptor="CLIENTE DEL EXTERIOR",
        tipo_cambio=cambio,
        monto_pen=monto_pen,
    )


def _fila(correlativo: int, monto: str) -> dict[str, object]:
    return {
        "id": uuid4(),
        "tipo_documento": "01",
        "serie": "FF02",
        "correlativo": correlativo,
        "monto_total": Decimal(monto),
    }


def test_el_total_en_soles_se_convierte_a_la_moneda_del_comprobante() -> None:
    (correccion,) = planificar_correcciones(
        [_registro(610, "68411.09", "3.402")], [_fila(610, "68411.09")]
    )

    assert correccion.documento == "FF02-00000610"
    assert correccion.monto_anterior == Decimal("68411.09")
    assert correccion.monto_total == Decimal("20109.08")
    assert correccion.monto_pen == Decimal("68411.09")
    assert correccion.tipo_cambio == Decimal("3.402")


def test_no_se_toca_lo_que_no_tiene_dato_utilizable_en_los_zip() -> None:
    registros = [_registro(611, "100.00", None), _registro(612, "100.00", "3.4")]
    filas = [_fila(611, "100.00"), _fila(613, "100.00")]  # 611 sin TC; 613 sin registro

    assert planificar_correcciones(registros, filas) == []
