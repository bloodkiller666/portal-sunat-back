"""Anota en cada compra el correo del que llegó (columna «Correo» de Compras).

Lee los registros que deja el extractor del buzón (`_correos/AAAA-MM/….json`) y los aplica a la
base con `app.services.correo_compras`. La automatización lo hace sola con lo que importa; este
script sirve para lo que ya estaba importado antes de existir la columna. Es idempotente y
conserva siempre el primer correo de cada compra. Sin `--aplicar` solo cuenta.

    uv run python -m scripts.cargar_correos                  # simulación
    uv run python -m scripts.cargar_correos --aplicar
    uv run python -m scripts.cargar_correos --destino C:/Users/yo/Downloads/FacturasProovedores
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from app.core.config import get_settings
from app.db.pool import Database
from app.db.uow import build_uow_factory
from app.services.companies import resolve_company_access
from app.services.correo_compras import CorreoDeCompra, clave_de_carpeta, registrar_correos


def leer_registro_correos(
    destino: Path, carpetas: set[str] | None = None
) -> dict[tuple[str, str, int], CorreoDeCompra]:
    """Un correo por compra (el más antiguo) a partir de `_correos/`.

    `carpetas` limita el resultado a esas carpetas (`AAAA/MM/RUC/SERIE-NÚMERO`). Un registro
    ilegible o sin fecha se salta: nunca detiene la carga.
    """
    resultado: dict[tuple[str, str, int], CorreoDeCompra] = {}
    base = destino / "_correos"
    if not base.is_dir():
        return resultado
    for ruta in sorted(base.rglob("*.json")):
        try:
            datos = json.loads(ruta.read_text(encoding="utf-8"))
            recibido = datetime.fromisoformat(datos["recibido"])
            asunto = str(datos.get("asunto") or "")
            remitente = str(datos.get("remitente") or "") or None
            rutas = [str(a["ruta"]) for a in datos.get("archivos", [])]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        for ruta_archivo in rutas:
            carpeta = ruta_archivo.rpartition("/")[0]
            if carpetas is not None and carpeta not in carpetas:
                continue
            clave = clave_de_carpeta(carpeta)
            if clave is None:  # _sin_clasificar, _otras_empresas, _originales_zip…
                continue
            previo = resultado.get(clave)
            if previo is None or recibido < previo.recibido_at:
                resultado[clave] = CorreoDeCompra(*clave, asunto, remitente, recibido)
    return resultado


async def ejecutar(args: argparse.Namespace) -> int:
    correos = await asyncio.to_thread(leer_registro_correos, Path(args.destino))
    con_asunto = [c for c in correos.values() if c.asunto.strip()]
    print(f"Correos con compra identificada: {len(correos)} ({len(con_asunto)} con asunto)")
    if not args.aplicar:
        print("Simulación: no se escribió nada. Use --aplicar.")
        return 0

    from scripts.automatizar import _usuario_de_servicio as usuario_de_servicio

    settings = get_settings()
    db = Database(settings)
    await db.connect()
    try:
        usuario, _ = await usuario_de_servicio(db, args.ruc, None)
        uow_factory = build_uow_factory(db)
        async with uow_factory(usuario.claims_para_rls) as uow:
            acceso = await resolve_company_access(uow, usuario, args.ruc)
            actualizadas = await registrar_correos(uow=uow, access=acceso, correos=con_asunto)
    finally:
        await db.close()
    print(f"Compras actualizadas: {actualizadas}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Anota el correo de origen en cada compra")
    parser.add_argument("--destino", default="~/Downloads/FacturasProveedores")
    parser.add_argument("--ruc", default="20514831620")
    parser.add_argument("--aplicar", action="store_true", help="Escribe de verdad (si no, simula)")
    args = parser.parse_args(argv)
    args.destino = str(Path(args.destino).expanduser().resolve())
    return asyncio.run(ejecutar(args))


if __name__ == "__main__":
    sys.exit(main())
