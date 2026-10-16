"""Respaldo incremental del bucket R2 a una carpeta local (evidencia de auditoría).

Los XML, CDR y ZIP de SIRE que guarda el portal son la evidencia ante SUNAT: tenerlos
en un único bucket es un punto único de fallo. Este script baja a una carpeta lo que
falte (o haya cambiado de tamaño) y deja un `manifiesto.csv` con el SHA-256 de cada
archivo, para poder comprobar después que nada se alteró.

    uv run python -m scripts.respaldar_r2 --destino D:\\respaldos\\portal-r2
    uv run python -m scripts.respaldar_r2 --destino ./respaldo --prefijo sire/

Es de solo lectura sobre R2 (nunca borra ni modifica nada). Se puede programar con el
Programador de tareas de Windows o cron. Las credenciales salen del `.env` y no se
imprimen.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings
from app.storage.providers.r2 import CloudflareR2StorageProvider

NOMBRE_MANIFIESTO = "manifiesto.csv"


@dataclass(frozen=True)
class ObjetoRemoto:
    clave: str
    tamano: int


def ruta_local(destino: Path, clave: str) -> Path:
    """Ruta dentro de `destino`; rechaza claves que intenten salirse de la carpeta."""
    candidata = (destino / clave).resolve()
    if destino.resolve() not in candidata.parents:
        raise ValueError(f"Clave de R2 no segura: {clave!r}")
    return candidata


def planificar(remotos: list[ObjetoRemoto], destino: Path) -> tuple[list[ObjetoRemoto], int]:
    """Qué falta bajar: lo que no existe o difiere en tamaño. Devuelve (pendientes, al_dia)."""
    pendientes: list[ObjetoRemoto] = []
    al_dia = 0
    for objeto in remotos:
        local = ruta_local(destino, objeto.clave)
        if local.is_file() and local.stat().st_size == objeto.tamano:
            al_dia += 1
        else:
            pendientes.append(objeto)
    return pendientes, al_dia


def sha256_de(ruta: Path) -> str:
    resumen = hashlib.sha256()
    with ruta.open("rb") as archivo:
        for bloque in iter(lambda: archivo.read(1024 * 1024), b""):
            resumen.update(bloque)
    return resumen.hexdigest()


def escribir_manifiesto(destino: Path, remotos: list[ObjetoRemoto]) -> int:
    """Manifiesto de todo lo que hay en la carpeta, con su hash actual."""
    filas = 0
    with (destino / NOMBRE_MANIFIESTO).open("w", newline="", encoding="utf-8") as salida:
        escritor = csv.writer(salida)
        escritor.writerow(["clave", "bytes", "sha256"])
        for objeto in sorted(remotos, key=lambda o: o.clave):
            local = ruta_local(destino, objeto.clave)
            if local.is_file():
                escritor.writerow([objeto.clave, local.stat().st_size, sha256_de(local)])
                filas += 1
    return filas


def _guardar(local: Path, contenido: bytes) -> None:
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(contenido)


async def respaldar(settings: Settings, destino: Path, prefijo: str) -> tuple[int, int, int]:
    proveedor = CloudflareR2StorageProvider(settings=settings)
    bucket = settings.r2_bucket_name
    await asyncio.to_thread(destino.mkdir, parents=True, exist_ok=True)

    remotos: list[ObjetoRemoto] = []
    async with proveedor._client() as r2:
        paginador = r2.get_paginator("list_objects_v2")
        async for pagina in paginador.paginate(Bucket=bucket, Prefix=prefijo):
            for objeto in pagina.get("Contents", []):
                if not objeto["Key"].endswith("/"):
                    remotos.append(ObjetoRemoto(objeto["Key"], int(objeto["Size"])))

        pendientes, al_dia = await asyncio.to_thread(planificar, remotos, destino)
        for objeto in pendientes:
            respuesta = await r2.get_object(Bucket=bucket, Key=objeto.clave)
            contenido = await respuesta["Body"].read()
            await asyncio.to_thread(_guardar, ruta_local(destino, objeto.clave), contenido)
            print(f"  + {objeto.clave} ({objeto.tamano} bytes)")

    filas = await asyncio.to_thread(escribir_manifiesto, destino, remotos)
    return len(pendientes), al_dia, filas


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Respaldo incremental de R2")
    parser.add_argument("--destino", required=True, help="Carpeta local del respaldo")
    parser.add_argument("--prefijo", default="", help="Solo claves con este prefijo")
    parser.add_argument("--env-file", default=".env", help="Ruta del archivo .env")
    args = parser.parse_args(argv)

    settings = Settings(_env_file=args.env_file)
    if not settings.r2_configured:
        print("ERROR: R2 no está configurado (R2_ACCOUNT_ID, claves y R2_BUCKET_NAME).")
        return 1

    nuevos, al_dia, filas = asyncio.run(respaldar(settings, Path(args.destino), args.prefijo))
    print(
        f"Respaldo listo: {nuevos} nuevos, {al_dia} ya estaban al día; "
        f"manifiesto con {filas} archivos."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
