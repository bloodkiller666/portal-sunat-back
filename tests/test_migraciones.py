"""Verificaciones estáticas de las migraciones.

No hay un Postgres disponible en las pruebas: se valida la estructura del SQL y,
sobre todo, que no exista ninguna columna para credenciales (regla 1).
"""

from __future__ import annotations

import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
MIGRACIONES = RAIZ / "supabase" / "migrations"

TABLAS = [
    "companies",
    "user_companies",
    "comprobantes",
    "comprobante_items",
    "sunat_envios",
    "archivos",
]


def _leer(nombre: str) -> str:
    return (MIGRACIONES / nombre).read_text(encoding="utf-8")


def _sql_completo() -> str:
    archivos = sorted(MIGRACIONES.glob("*.sql"))
    return "\n".join(archivo.read_text(encoding="utf-8") for archivo in archivos)


def test_existen_las_migraciones_en_orden() -> None:
    nombres = sorted(archivo.name for archivo in MIGRACIONES.glob("*.sql"))
    assert nombres == [
        "0001_schema.sql",
        "0002_functions_triggers.sql",
        "0003_rls.sql",
        "0004_archivos_storage.sql",
        "0005_comprobantes_recibidos.sql",
        "0006_comprobante_pago.sql",
        "0007_comprobante_fuente.sql",
        "0008_rendimiento_listado.sql",
        "0009_bizlinks_consultas.sql",
        "0010_compras_unique_constraint.sql",
        "0011_tipo_cambio_monto_pen.sql",
        "0012_emisor_tipo_doc.sql",
        "0013_automatizacion_logs.sql",
        "0014_comprobante_correo.sql",
    ]


def test_estan_las_tablas_del_alcance() -> None:
    sql = _sql_completo()
    for tabla in TABLAS:
        assert f"create table public.{tabla} " in sql, f"Falta la tabla {tabla}"


def test_todas_las_tablas_crean_los_tipos_del_dominio() -> None:
    sql = _leer("0001_schema.sql")
    for tipo in (
        "public.sunat_ambiente",
        "public.rol_empresa",
        "public.estado_comprobante",
        "public.tipo_operacion_sunat",
        "public.tipo_archivo",
        "public.estado_archivo",
    ):
        assert f"create type {tipo}" in sql

    assert "('pendiente', 'aprobado', 'rechazado', 'anulado')" in sql


def test_rls_habilitado_en_todas_las_tablas() -> None:
    sql = _leer("0003_rls.sql")
    for tabla in TABLAS:
        assert f"alter table public.{tabla} enable row level security;" in sql


def test_cada_tabla_tiene_politicas() -> None:
    sql = _leer("0003_rls.sql")
    for tabla in TABLAS:
        assert f"on public.{tabla} for select" in sql, f"{tabla} sin política de lectura"


def test_las_politicas_usan_los_helpers_y_no_recursionan() -> None:
    sql = _leer("0003_rls.sql")
    # Consultar user_companies dentro de una política de user_companies produce
    # recursión infinita en Postgres: los helpers SECURITY DEFINER lo evitan.
    assert "from public.user_companies" not in sql
    assert "app.is_company_member(" in sql
    assert "app.has_company_role(" in sql


def test_helpers_de_rls_son_security_definer() -> None:
    sql = _leer("0002_functions_triggers.sql")
    assert sql.count("security definer") == 2
    assert "create or replace function app.is_company_member(p_company_id uuid)" in sql
    assert "create or replace function app.has_company_role(" in sql
    assert "set search_path = public, auth, pg_temp" in sql
    assert "grant execute on function app.is_company_member" in sql


def test_sunat_envios_es_append_only() -> None:
    sql = _leer("0003_rls.sql")
    assert "on public.sunat_envios for insert" in sql
    assert "on public.sunat_envios for update" not in sql
    assert "on public.sunat_envios for delete" not in sql


def test_sunat_envios_usa_identity_y_guarda_lo_necesario() -> None:
    sql = _leer("0001_schema.sql")
    assert "id                bigint generated always as identity primary key" in sql
    for columna in (
        "tipo_operacion",
        "sunat_ambiente",
        "request_xml",
        "response_xml",
        "codigo_respuesta",
        "mensaje_respuesta",
        "notas",
        "exito",
        "duracion_ms",
    ):
        assert columna in sql
    assert "WS-Security eliminado" in sql


def test_comprobantes_tiene_los_campos_del_frontend() -> None:
    sql = _leer("0001_schema.sql")
    for columna in (
        "n_sap",
        "orden_compra",
        "check_sap",
        "cdr_codigo",
        "cdr_descripcion",
        "cdr_notas",
        "nombre_archivo",
        "estado",
        "motivo_anulacion",
    ):
        assert columna in sql


def test_archivos_referencian_el_objeto_en_r2() -> None:
    sql = _leer("0004_archivos_storage.sql")
    for columna in ("storage_provider", "storage_bucket", "storage_key"):
        assert f"add column if not exists {columna}" in sql
    assert "URL firmada" in sql


def test_comprobantes_distinguen_emitidos_de_recibidos() -> None:
    sql = _leer("0005_comprobantes_recibidos.sql")
    assert "create type public.origen_comprobante as enum ('emitido', 'recibido')" in sql
    for columna in ("origen", "ruc_receptor", "receptor_doc", "razon_social_emisor"):
        assert f"add column {columna}" in sql
    # La unicidad debe separar ventas de compras: la misma numeración puede
    # existir como emitida y como recibida.
    assert "unique (company_id, origen, tipo_documento, serie, correlativo, sunat_ambiente)" in sql


def test_no_hay_columnas_de_credenciales() -> None:
    sql = _sql_completo().lower()
    for prohibido in (
        "sol_password",
        "sol_user",
        "cert_password",
        "certificado_pfx",
        "client_secret",
        "client_id",
    ):
        # Ninguna columna (declaración de tipo) puede llevar credenciales.
        assert not re.search(rf"^\s+{prohibido}\s+(text|bytea|varchar|char)", sql, re.MULTILINE), (
            f"La base no debe guardar {prohibido}"
        )


def test_comprobantes_se_unen_por_empresa_y_rol() -> None:
    sql = _leer("0003_rls.sql")
    assert "array['admin', 'emisor']::public.rol_empresa[]" in sql
    assert "array['admin']::public.rol_empresa[]" in sql
    assert "array['lector']" not in sql  # el lector nunca escribe


def test_nomenclatura_sunat_se_completa_con_trigger() -> None:
    sql = _leer("0002_functions_triggers.sql")
    assert "lpad(new.correlativo::text, 8, '0')" in sql
    assert "new.ruc_emisor || '-' || new.tipo_documento || '-' || new.serie" in sql


def test_migracion_0006_agrega_las_condiciones_de_pago() -> None:
    sql = _leer("0006_comprobante_pago.sql")
    assert "add column fecha_vencimiento date" in sql
    assert "add column forma_pago text" in sql
    assert sql.count("begin;") == 1 and sql.count("commit;") == 1
