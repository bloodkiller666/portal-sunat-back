from __future__ import annotations

import pytest

from app.core.enums import EstadoComprobante
from app.sunat.models import CdrInfo
from app.sunat.states import map_baja_a_estado, map_cdr_a_estado, map_codigo_a_estado


def _cdr(codigo: str, descripcion: str = "") -> CdrInfo:
    return CdrInfo(codigo=codigo, descripcion=descripcion)


@pytest.mark.parametrize(
    ("codigo", "esperado"),
    [
        ("0", EstadoComprobante.aprobado),
        ("98", EstadoComprobante.pendiente),
        ("2025", EstadoComprobante.rechazado),
        ("2335", EstadoComprobante.rechazado),
        ("0104", EstadoComprobante.rechazado),
        (None, EstadoComprobante.pendiente),
    ],
)
def test_tabla_de_codigos(codigo: str | None, esperado: EstadoComprobante) -> None:
    assert map_codigo_a_estado(codigo) is esperado


@pytest.mark.parametrize(
    "mensaje",
    [
        "El comprobante está en proceso",
        "El comprobante no existe",
        "La factura no se encontró en los registros",
        "El comprobante no ha sido informado",
    ],
)
def test_codigo_desconocido_con_mensaje_de_pendiente(mensaje: str) -> None:
    assert map_codigo_a_estado("1033", mensaje) is EstadoComprobante.pendiente


def test_codigo_desconocido_sin_mensaje_se_rechaza() -> None:
    assert map_codigo_a_estado("9999") is EstadoComprobante.rechazado


def test_etiquetas_del_frontend() -> None:
    assert map_cdr_a_estado(_cdr("0")).etiqueta == "Aprobado"
    assert map_cdr_a_estado(_cdr("98")).etiqueta == "Pendiente"
    assert map_cdr_a_estado(_cdr("2025")).etiqueta == "Rechazado"


def test_baja_aceptada_anula_el_comprobante() -> None:
    assert map_baja_a_estado(_cdr("0")) is EstadoComprobante.anulado
    assert map_cdr_a_estado(_cdr("0"), es_comunicacion_baja=True) is EstadoComprobante.anulado
    assert map_cdr_a_estado(_cdr("2025"), es_comunicacion_baja=True) is EstadoComprobante.rechazado


def test_la_baja_no_afecta_el_mapeo_de_comprobantes() -> None:
    assert map_cdr_a_estado(_cdr("0")) is EstadoComprobante.aprobado
