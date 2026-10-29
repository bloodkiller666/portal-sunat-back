"""Pasa las rutas de R2 con el mes en número (`2026/10`) al mes en letras (`2026/OCTUBRE`).

    comprobantes/RUC/2026/10/FF01-1/x.xml  a  comprobantes/RUC/2026/OCTUBRE/FF01-1/x.xml

Por defecto solo **simula**. Con `--aplicar` copia cada objeto, corrige `archivos.storage_key` y
recién entonces borra el original (nunca se borra algo que la base aún no apunta a su ruta nueva).
Es idempotente: lo que ya está en letras no se toca. Las evidencias de SIRE (`sire/RUC/AAAAMM/`)
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
from app.core.meses import nombre_mes
from app.storage.providers.r2 import CloudflareR2StorageProvider

_CLAVE = re.compile(r"^(comprobantes/\d{11}/\d{4}/)(0[1-9]|1[0-2])(/.+)$")


def clave_en_letras(clave: str) -> str | None:
    """La clave con el mes en letras, o `None` si ya lo está o no tiene esa forma."""
    m = _CLAVE.match(clave)
    return f"{m.group(1)}{nombre_mes(int(m.group(2)))}{m.group(3)}" if m else None


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
            plan = [(k, n) for k in sorted(existentes) if (n := clave_en_letras(k))]
            print(f"{len(plan)} objetos con el mes en número de {len(existentes)} en el bucket")
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
    parser = argparse.ArgumentParser(description="Meses en letras en las rutas de R2")
    parser.add_argument("--aplicar", action="store_true", help="Mueve de verdad (si no, simula)")
    parser.add_argument("--env-file", default=".env")
    return asyncio.run(_ejecutar(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
