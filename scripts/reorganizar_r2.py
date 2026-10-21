"""Reorganiza el bucket R2 al esquema `AÑO/MES/N° DOCUMENTO` sin perder nada.

Antes cada mes amontonaba todos sus XML y CDR en una sola carpeta:

    comprobantes/20514831620/2026/10/20514831620-01-FF01-00015980.xml
    comprobantes/20514831620/2026/10/20514831620-01-FF01-00015980-CDR.zip

Ahora cada documento tiene su carpeta y el servidor ya sube así los archivos
nuevos (`app/services/store.py`). Los objetos que ya estaban en el bucket se
mueven con este script, que copia y después corrige `archivos.storage_key`:

    comprobantes/20514831620/2026/10/FF01-00015980/20514831620-01-FF01-00015980.xml
    comprobantes/20514831620/2026/10/FF01-00015980/20514831620-01-FF01-00015980-CDR.zip

Por defecto solo **simula** (no escribe ni en R2 ni en la base). Para ejecutarlo:

    uv run python -m scripts.reorganizar_r2                 # plan
    uv run python -m scripts.reorganizar_r2 --aplicar       # copia + actualiza la base
    uv run python -m scripts.reorganizar_r2 --aplicar --borrar-originales

Los originales se conservan salvo que se pida `--borrar-originales`; se borran
solo después de que la copia y la fila de `archivos` estén actualizadas. Las
URLs firmadas se firman con la clave guardada en la base, así que mientras esa
fila no se actualice la descarga sigue funcionando con la ruta vieja: nunca se
borra algo que todavía no se puede descargar desde la ruta nueva.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass

import asyncpg
from scripts.aplicar_migraciones import normalizar_dsn

from app.core.config import Settings
from app.core.redaction import redact
from app.storage.providers.r2 import CloudflareR2StorageProvider

PREFIJO_POR_DEFECTO = "comprobantes/"
_EXTENSION = re.compile(r"\.(xml|pdf|zip)$", re.IGNORECASE)
_SUFIJO_CDR = re.compile(r"-CDR$", re.IGNORECASE)


@dataclass(frozen=True)
class Movimiento:
    """Un objeto que cambia de ruta (copia en R2 + fila en `archivos`)."""

    origen: str
    destino: str


def clave_reorganizada(clave: str) -> str | None:
    """Clave con la carpeta del documento, o `None` si ya está o no aplica.

    El número del documento se lee del nombre, con la nomenclatura de SUNAT
    `{RUC}-{TIPO}-{SERIE}-{NÚMERO}` (el CDR añade `-CDR`). Si el nombre no sigue
    esa forma se deja como está: es preferible no mover nada a inventar la ruta.
    """
    partes = clave.split("/")
    if len(partes) != 5 or partes[0] != "comprobantes":
        return None  # ya tiene su carpeta (6 niveles), o no es de comprobantes
    ruc, anio, mes, nombre = partes[1:]
    if not (ruc.isdigit() and len(anio) == 4 and anio.isdigit() and mes.isdigit()):
        return None

    base = _SUFIJO_CDR.sub("", _EXTENSION.sub("", nombre))
    trozos = base.split("-")
    if len(trozos) != 4 or not trozos[3].isdigit():
        return None
    _, _, serie, numero = trozos
    return f"comprobantes/{ruc}/{anio}/{mes}/{serie}-{numero}/{nombre}"


async def planificar(
    proveedor: CloudflareR2StorageProvider, bucket: str, prefijo: str
) -> tuple[list[Movimiento], list[str], set[str]]:
    """Lee el bucket y devuelve (movimientos, ignoradas, claves existentes)."""
    movimientos: list[Movimiento] = []
    ignoradas: list[str] = []
    existentes: set[str] = set()
    async with proveedor._client() as r2:
        paginador = r2.get_paginator("list_objects_v2")
        async for pagina in paginador.paginate(Bucket=bucket, Prefix=prefijo):
            for objeto in pagina.get("Contents", []):
                clave = objeto["Key"]
                if clave.endswith("/"):
                    continue
                existentes.add(clave)
                destino = clave_reorganizada(clave)
                if destino is None:
                    ignoradas.append(clave)
                else:
                    movimientos.append(Movimiento(clave, destino))
    return movimientos, ignoradas, existentes


async def aplicar(
    settings: Settings,
    movimientos: list[Movimiento],
    existentes: set[str],
    *,
    borrar_originales: bool,
    dsn: str | None,
) -> tuple[int, int, int]:
    """Copia cada objeto a su nueva ruta y actualiza `archivos.storage_key`.

    Devuelve (copiados, sin_fila, borrados). Es idempotente: si el destino ya
    existe, no se vuelve a copiar (sí se corrige la fila, si quedó apuntando al
    original).
    """
    proveedor = CloudflareR2StorageProvider(settings=settings)
    bucket = settings.r2_bucket_name or ""
    conexion = None
    if dsn:
        dsn_limpio, ssl = normalizar_dsn(dsn)
        conexion = await asyncpg.connect(dsn_limpio, ssl=ssl)

    copiados = sin_fila = borrados = 0
    try:
        async with proveedor._client() as r2:
            for movimiento in movimientos:
                if movimiento.destino not in existentes:
                    await r2.copy_object(
                        Bucket=bucket,
                        Key=movimiento.destino,
                        CopySource={"Bucket": bucket, "Key": movimiento.origen},
                    )
                    copiados += 1
                # La descarga usa `archivos.storage_key`: si esta fila no se
                # actualiza, el archivo queda inaccesible desde la ruta nueva.
                fila = None
                if conexion is not None:
                    fila = await conexion.fetchval(
                        """
                        update public.archivos set storage_key = $1
                        where storage_key = $2
                        returning id
                        """,
                        movimiento.destino,
                        movimiento.origen,
                    )
                if fila is None:
                    sin_fila += 1
                    print(f"  ! sin fila en archivos: {movimiento.origen}")
                if borrar_originales:
                    await r2.delete_object(Bucket=bucket, Key=movimiento.origen)
                    borrados += 1
                print(f"  {'=' if fila else '+'} {movimiento.destino}")
    finally:
        if conexion is not None:
            await conexion.close()
    return copiados, sin_fila, borrados


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reorganiza las rutas del bucket R2")
    parser.add_argument(
        "--aplicar",
        action="store_true",
        help="Copia de verdad y actualiza la base (por defecto, solo simula)",
    )
    parser.add_argument(
        "--borrar-originales",
        action="store_true",
        help="Borra la ruta vieja después de copiar y actualizar la fila",
    )
    parser.add_argument(
        "--prefijo", default=PREFIJO_POR_DEFECTO, help="Solo claves con este prefijo"
    )
    parser.add_argument("--env-file", default=".env", help="Ruta del archivo .env")
    args = parser.parse_args(argv)

    settings = Settings(_env_file=args.env_file)
    if not settings.r2_configured:
        print("ERROR: R2 no está configurado (R2_ACCOUNT_ID, claves y R2_BUCKET_NAME).")
        return 1
    dsn = None
    if args.aplicar:
        if not settings.database_configured or settings.database_url is None:
            print("ERROR: sin DATABASE_URL no se puede corregir archivos.storage_key.")
            return 1
        dsn = settings.database_url.get_secret_value()
    if args.borrar_originales and dsn is None:
        print("ERROR: --borrar-originales exige --aplicar y DATABASE_URL.")
        return 1

    proveedor = CloudflareR2StorageProvider(settings=settings)
    try:
        movimientos, ignoradas, existentes = asyncio.run(
            planificar(proveedor, settings.r2_bucket_name or "", args.prefijo)
        )
    except Exception as exc:  # el bucket no responde o las credenciales no sirven
        print(f"ERROR al listar el bucket: {type(exc).__name__}: {redact(str(exc))[:200]}")
        return 1

    print(f"Objetos a mover: {len(movimientos)}; fuera de este esquema: {len(ignoradas)}")
    for clave in ignoradas[:10]:
        print(f"  (se deja igual) {clave}")
    if not args.aplicar:
        for movimiento in movimientos[:20]:
            print(f"  {movimiento.origen} -> {movimiento.destino}")
        if len(movimientos) > 20:
            print(f"  ... y {len(movimientos) - 20} más")
        print("Simulación: nada se escribió. Repita con --aplicar.")
        return 0

    try:
        copiados, sin_fila, borrados = asyncio.run(
            aplicar(
                settings,
                movimientos,
                existentes,
                borrar_originales=args.borrar_originales,
                dsn=dsn,
            )
        )
    except Exception as exc:
        print(f"ERROR al mover: {type(exc).__name__}: {redact(str(exc))[:200]}")
        return 1
    print(
        f"Listo: {copiados} copiados, {sin_fila} sin fila en archivos, "
        f"{borrados} originales borrados."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
