"""Construcción del SQL del listado (sin base de datos): parámetros, escape y lista blanca."""

from __future__ import annotations

import re
from datetime import date
from uuid import uuid4

import pytest

from app.core.enums import (
    CampoOrden,
    DireccionOrden,
    EstadoComprobante,
    OrigenComprobante,
    RespaldoFiltro,
)
from app.db.repositories.base import FiltrosListado
from app.db.repositories.postgres import _donde, _orden, _patron_like


def test_sin_filtros_solo_acota_por_empresa() -> None:
    empresa = uuid4()
    donde, parametros = _donde(empresa, FiltrosListado())
    assert donde == "c.company_id = $1"
    assert parametros == [empresa]


def test_los_valores_van_como_parametros_nunca_en_el_texto() -> None:
    empresa = uuid4()
    filtros = FiltrosListado(
        estado=EstadoComprobante.aprobado,
        fecha_desde=date(2026, 9, 1),
        fecha_hasta=date(2026, 9, 30),
        tipo_documento="01",
        serie="FF01",
        moneda="PEN",
        vencimiento_desde=date(2026, 10, 1),
        vencimiento_hasta=date(2026, 10, 31),
        n_sap="4500",
        q="x'; drop table comprobantes; --",
    )
    donde, parametros = _donde(empresa, filtros)

    assert "drop table" not in donde
    assert "FF01" not in donde and "aprobado" not in donde
    # $1 es la empresa; cada filtro agrega un parámetro con su número consecutivo.
    assert parametros[0] == empresa
    assert parametros[1] == "aprobado"
    assert date(2026, 9, 1) in parametros and date(2026, 9, 30) in parametros
    assert date(2026, 10, 1) in parametros and date(2026, 10, 31) in parametros
    assert "coalesce(c.fecha_vencimiento, c.fecha_emision) >=" in donde
    numeros = {int(n) for n in re.findall(r"\$(\d+)", donde)}
    assert numeros == set(range(1, len(parametros) + 1))


def test_los_comodines_del_usuario_se_escapan() -> None:
    assert _patron_like("100%_a\\b") == "%100\\%\\_a\\\\b%"


def test_la_busqueda_cubre_documento_cliente_ruc_y_orden_de_compra() -> None:
    donde, parametros = _donde(uuid4(), FiltrosListado(q="andes"))
    assert parametros[-1] == "%andes%"
    for fragmento in (
        "c.serie || '-' || c.numero",
        "razon_social_receptor",
        "ruc_receptor",
        "orden_compra",
    ):
        assert fragmento in donde
    # En ventas, el nombre del emisor es la propia empresa: no se busca ahí.
    assert "razon_social_emisor" not in donde


def test_en_compras_la_busqueda_incluye_al_proveedor() -> None:
    donde, _ = _donde(
        uuid4(), FiltrosListado(q="proveedor", origen=OrigenComprobante.recibido)
    )
    assert "razon_social_emisor" in donde and "c.ruc_emisor" in donde


@pytest.mark.parametrize(
    ("respaldo", "esperado"),
    [
        (RespaldoFiltro.sin_xml, "not c.tiene_xml"),
        (RespaldoFiltro.sin_cdr, "not c.tiene_cdr"),
        (RespaldoFiltro.completo, "c.tiene_xml and c.tiene_cdr"),
    ],
)
def test_el_respaldo_usa_las_columnas_desnormalizadas(
    respaldo: RespaldoFiltro, esperado: str
) -> None:
    donde, _ = _donde(uuid4(), FiltrosListado(respaldo=respaldo))
    assert esperado in donde
    assert "public.archivos" not in donde  # sin subconsulta por fila


@pytest.mark.parametrize("campo", list(CampoOrden))
@pytest.mark.parametrize("direccion", list(DireccionOrden))
def test_el_orden_siempre_desempata_por_id_en_el_mismo_sentido(
    campo: CampoOrden, direccion: DireccionOrden
) -> None:
    sql = _orden(FiltrosListado(orden=campo, direccion=direccion))
    assert sql.endswith(f"c.id {direccion.value}")
    # Solo texto de la lista blanca: nada que venga del usuario.
    assert ";" not in sql and "--" not in sql


def test_la_fecha_ordena_como_el_indice_recorrido_al_reves() -> None:
    desc = _orden(FiltrosListado(orden=CampoOrden.fecha, direccion=DireccionOrden.desc))
    asc = _orden(FiltrosListado(orden=CampoOrden.fecha, direccion=DireccionOrden.asc))
    assert desc == "c.fecha_emision desc nulls last, c.correlativo desc, c.id desc"
    assert asc == "c.fecha_emision asc nulls first, c.correlativo asc, c.id asc"


def test_el_importe_resta_las_notas_de_credito() -> None:
    sql = _orden(FiltrosListado(orden=CampoOrden.importe))
    assert "tipo_documento = '07' then -c.monto_total" in sql


def test_los_clientes_sin_nombre_van_al_final() -> None:
    for direccion in DireccionOrden:
        sql = _orden(FiltrosListado(orden=CampoOrden.cliente, direccion=direccion))
        assert "nullif(" in sql and "nulls last" in sql
