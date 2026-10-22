"""Corrige los montos en moneda extranjera de las VENTAS ya cargadas desde SIRE.

SIRE informa `Total CP` en soles (convertido con `Tipo Cambio`) aunque `Moneda` diga USD, y
antes de la migración 0011 el portal guardaba ese total tal cual como si fueran dólares. Este
script relee los ZIP de evidencia archivados en R2 (`sire/{RUC}/{PERIODO}/`), que conservan el
tipo de cambio de cada fila, y para cada venta en moneda extranjera sin `tipo_cambio`:

    monto_total = total en soles / tipo de cambio      (en la moneda del comprobante)
    monto_pen   = total en soles, tal como lo informó SIRE
    tipo_cambio = el de SIRE

También rellena `receptor_tipo_doc` (0 = cliente del exterior, 1 = DNI...) de las ventas ya
cargadas, que antes no se guardaba: así el portal distingue un DNI o un cliente extranjero de un
RUC faltante.

Requisito: haber aplicado las migraciones `0011` y `0012`.

Por defecto solo **simula** (no escribe en la base):

    uv run python -m scripts.corregir_montos_sire --ruc 20514831620            # plan
    uv run python -m scripts.corregir_montos_sire --ruc 20514831620 --aplicar  # actualiza

Es idempotente: una fila que ya tiene `tipo_cambio` no se vuelve a tocar.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import asyncpg
from scripts.aplicar_migraciones import normalizar_dsn

from app.core.config import Settings
from app.core.redaction import redact
from app.storage.providers.r2 import CloudflareR2StorageProvider
from app.sunat.sire_rvie import RegistroSire, parse_propuesta_zip


@dataclass(frozen=True)
class Correccion:
    comprobante_id: Any
    documento: str
    moneda: str
    monto_anterior: Decimal | None
    monto_total: Decimal
    monto_pen: Decimal
    tipo_cambio: Decimal


def planificar_correcciones(
    registros: list[RegistroSire], filas: list[dict[str, Any]]
) -> list[Correccion]:
    """Cruza lo que informó SIRE con las filas por corregir (puro: no toca la base).

    `filas` trae `id, tipo_documento, serie, correlativo, monto_total` de ventas en moneda
    extranjera sin `tipo_cambio`. Una fila sin su registro en los ZIP, o cuyo registro no
    trae un tipo de cambio utilizable, no se toca.
    """
    por_clave = {
        (r.tipo_documento, r.serie, r.correlativo): r
        for r in registros
        if r.tipo_cambio is not None and r.monto_pen is not None
    }
    correcciones: list[Correccion] = []
    for fila in filas:
        registro = por_clave.get((fila["tipo_documento"], fila["serie"], fila["correlativo"]))
        if registro is None or registro.monto_pen is None or registro.tipo_cambio is None:
            continue
        correcciones.append(
            Correccion(
                comprobante_id=fila["id"],
                documento=f"{fila['serie']}-{fila['correlativo']:08d}",
                moneda=registro.moneda,
                monto_anterior=fila["monto_total"],
                monto_total=registro.monto_total,
                monto_pen=registro.monto_pen,
                tipo_cambio=registro.tipo_cambio,
            )
        )
    return correcciones


async def _leer_registros(settings: Settings, claves: list[str], ruc: str) -> list[RegistroSire]:
    proveedor = CloudflareR2StorageProvider(settings=settings)
    registros: list[RegistroSire] = []
    async with proveedor._client() as r2:
        for clave in claves:
            objeto = await r2.get_object(Bucket=settings.r2_bucket_name or "", Key=clave)
            contenido = await objeto["Body"].read()
            registros.extend(parse_propuesta_zip(contenido, ruc_esperado=ruc).registros)
    return registros


async def ejecutar(args: argparse.Namespace) -> int:
    settings = Settings(_env_file=args.env_file)
    if not settings.database_configured or settings.database_url is None:
        print("ERROR: falta DATABASE_URL.")
        return 2
    dsn, ssl = normalizar_dsn(settings.database_url.get_secret_value())
    conexion = await asyncpg.connect(dsn, ssl=ssl)
    try:
        existe = await conexion.fetchval(
            "select count(*) from information_schema.columns "
            "where table_name = 'comprobantes' and column_name = 'tipo_cambio'"
        )
        if not existe:
            print("ERROR: falta aplicar la migración 0011_tipo_cambio_monto_pen.sql.")
            return 2
        company_id = await conexion.fetchval(
            "select id from public.companies where ruc = $1", args.ruc
        )
        if company_id is None:
            print(f"ERROR: no existe la empresa {args.ruc}.")
            return 2
        filas = [
            dict(f)
            for f in await conexion.fetch(
                """
                select id, tipo_documento, serie, correlativo, monto_total
                  from public.comprobantes
                 where company_id = $1 and origen = 'emitido' and fuente = 'sire'
                   and moneda <> 'PEN' and tipo_cambio is null
                """,
                company_id,
            )
        ]
        claves = [
            f["storage_key"]
            for f in await conexion.fetch(
                """
                select storage_key from public.archivos
                 where company_id = $1 and tipo = 'zip' and storage_key like $2
                """,
                company_id,
                f"sire/{args.ruc}/%",
            )
        ]
        print(f"Ventas en moneda extranjera por corregir: {len(filas)}")
        print(f"ZIP de evidencia en R2: {len(claves)}")
        registros = await _leer_registros(settings, claves, args.ruc)
        correcciones = planificar_correcciones(registros, filas)
        for c in correcciones[: args.mostrar]:
            print(
                f"  {c.documento}: {c.monto_anterior} -> {c.monto_total} {c.moneda} "
                f"(S/ {c.monto_pen} / TC {c.tipo_cambio})"
            )
        sin_dato = len(filas) - len(correcciones)
        print(f"Con corrección: {len(correcciones)} | sin dato en los ZIP: {sin_dato}")
        tipos = {
            (r.tipo_documento, r.serie, r.correlativo): r.receptor_tipo_doc
            for r in registros
            if r.receptor_tipo_doc
        }
        sin_tipo = [
            dict(f)
            for f in await conexion.fetch(
                """
                select id, tipo_documento, serie, correlativo from public.comprobantes
                 where company_id = $1 and origen = 'emitido' and fuente = 'sire'
                   and receptor_tipo_doc is null
                """,
                company_id,
            )
        ]
        con_tipo = [
            (f["id"], tipos[(f["tipo_documento"], f["serie"], f["correlativo"])])
            for f in sin_tipo
            if (f["tipo_documento"], f["serie"], f["correlativo"]) in tipos
        ]
        print(f"Tipo de documento del receptor por rellenar: {len(con_tipo)} de {len(sin_tipo)}")
        if not args.aplicar:
            print("Simulación: nada se escribió. Repita con --aplicar.")
            return 0
        async with conexion.transaction():
            for c in correcciones:
                await conexion.execute(
                    "update public.comprobantes set monto_total = $2, monto_pen = $3, "
                    "tipo_cambio = $4 where id = $1 and tipo_cambio is null",
                    c.comprobante_id,
                    c.monto_total,
                    c.monto_pen,
                    c.tipo_cambio,
                )
            for comprobante_id, tipo in con_tipo:
                await conexion.execute(
                    "update public.comprobantes set receptor_tipo_doc = $2 "
                    "where id = $1 and receptor_tipo_doc is null",
                    comprobante_id,
                    tipo,
                )
        print(f"Montos actualizados: {len(correcciones)} | tipos de documento: {len(con_tipo)}")
        return 0
    finally:
        await conexion.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Corrige montos en USD de ventas SIRE")
    parser.add_argument("--ruc", required=True, help="RUC de la empresa")
    parser.add_argument("--aplicar", action="store_true", help="Escribe en la base (si no, simula)")
    parser.add_argument("--mostrar", type=int, default=10, help="Filas de ejemplo a imprimir")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(ejecutar(args))
    except Exception as exc:
        print("ERROR:", redact(str(exc)))
        return 1


if __name__ == "__main__":
    sys.exit(main())
