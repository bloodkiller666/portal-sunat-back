"""Pasa las rutas de R2 al mes con número y nombre (`2026/10. OCTUBRE`).

    comprobantes/RUC/2026/10/FF01-1/x.xml       a  comprobantes/RUC/2026/10. OCTUBRE/FF01-1/x.xml
    comprobantes/RUC/2026/OCTUBRE/FF01-1/x.xml  a  comprobantes/RUC/2026/10. OCTUBRE/FF01-1/x.xml

Por defecto solo **simula**. Con `--aplicar` copia cada objeto, corrige `archivos.storage_key` y
recién entonces borra el original (nunca se borra algo que la base aún no apunta a su ruta nueva).
Es idempotente: lo que ya está en `10. OCTUBRE` no se toca. Las evidencias de SIRE
(`sire/RUC/AAAAMM/`)
llevan el periodo, no una carpeta de mes, y no se mueven.

    uv run python -m scripts.meses_en_letras_r2             # plan
    uv run python -m scripts.meses_en_letras_r2 --aplicar
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys

import asyncpg
from scripts.aplicar_migraciones import normalizar_dsn

from app.core.config import Settings
from app.core.meses import carpeta_de_mes, numero_de_mes
from app.storage.providers.r2 import CloudflareR2StorageProvider

_CLAVE = re.compile(r"^(comprobantes/\d{11}/\d{4}/)([^/]+)(/.+)$")


def clave_con_mes_actual(clave: str) -> str | None:
    """La clave con el mes como `10. OCTUBRE`, o `None` si ya lo está o no tiene esa forma.

    El mes de antes puede ser el número (`10`) o el nombre solo (`OCTUBRE`).
    """
    m = _CLAVE.match(clave)
    mes = numero_de_mes(m.group(2)) if m else None
    if m is None or mes is None or m.group(2) == carpeta_de_mes(mes):
        return None
    return f"{m.group(1)}{carpeta_de_mes(mes)}{m.group(3)}"


async def _ejecutar(args: argparse.Namespace) -> int:
    settings = Settings(_env_file=args.env_file)
    if not settings.r2_configured or settings.database_url is None:
        print("ERROR: faltan R2 o DATABASE_URL en el .env")
        return 1
    proveedor = CloudflareR2StorageProvider(settings=settings)
    bucket = settings.r2_bucket_name
    dsn, ssl = normalizar_dsn(settings.database_url.get_secret_value())
    conexion = await asyncpg.connect(dsn, ssl=ssl)
    try:
        async with proveedor._client() as r2:
            existentes: set[str] = set()
            async for pagina in r2.get_paginator("list_objects_v2").paginate(Bucket=bucket):
                existentes.update(o["Key"] for o in pagina.get("Contents", []))
            plan = [(k, n) for k in sorted(existentes) if (n := clave_con_mes_actual(k))]
            print(f"{len(plan)} objetos con el mes anterior de {len(existentes)} en el bucket")
            if not args.aplicar:
                for origen, destino in plan[:5]:
                    print(f"  {origen}\n    a {destino}")
                print("Simulación: nada se movió. Usa --aplicar.")
                return 0
            movidos = sin_fila = 0
            for origen, destino in plan:
                if destino not in existentes:
                    await r2.copy_object(
                        Bucket=bucket,
                        Key=destino,
                        CopySource={"Bucket": bucket, "Key": origen},
                    )
                actualizadas = await conexion.execute(
                    "update public.archivos set storage_key = $1 where storage_key = $2",
                    destino,
                    origen,
                )
                if actualizadas == "UPDATE 0":
                    sin_fila += 1  # objeto sin fila en la base: se conserva también el original
                    continue
                await r2.delete_object(Bucket=bucket, Key=origen)
                movidos += 1
            print(f"Movidos: {movidos}. Sin fila en la base (original conservado): {sin_fila}.")
    finally:
        await conexion.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Meses como «10. OCTUBRE» en las rutas de R2")
    parser.add_argument("--aplicar", action="store_true", help="Mueve de verdad (si no, simula)")
    parser.add_argument("--env-file", default=".env")
    return asyncio.run(_ejecutar(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
