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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

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


@dataclass(frozen=True)
class CorreoDePdfSuelto:
    """Un PDF que llegó sin XML (queda en `_sin_clasificar`) y el correo que lo trajo."""

    ruta: str
    asunto: str
    remitente: str | None
    recibido_at: datetime


def leer_correos_de_pdf_sueltos(
    destino: Path, carpetas: set[str] | None = None
) -> list[CorreoDePdfSuelto]:
    """Los PDF de `_sin_clasificar` con su correo, a partir de `_correos/`.

    Un PDF sin XML no tiene carpeta de compra, así que `leer_registro_correos` no lo vincula: aquí
    se devuelven para identificar su compra por el texto del PDF. `carpetas` limita el resultado a
    esas carpetas (`_sin_clasificar/AAAA-MM/{correo}`).
    """
    resultado: list[CorreoDePdfSuelto] = []
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
            normal = ruta_archivo.replace("\\", "/")
            if not normal.startswith("_sin_clasificar/") or not normal.lower().endswith(".pdf"):
                continue
            if carpetas is not None and normal.rpartition("/")[0] not in carpetas:
                continue
            resultado.append(CorreoDePdfSuelto(normal, asunto, remitente, recibido))
    return resultado


async def ejecutar(args: argparse.Namespace) -> int:
    correos = await asyncio.to_thread(leer_registro_correos, Path(args.destino))
    con_asunto = [c for c in correos.values() if c.asunto.strip()]
    sueltos = await asyncio.to_thread(leer_correos_de_pdf_sueltos, Path(args.destino))
    print(f"Correos con compra identificada: {len(correos)} ({len(con_asunto)} con asunto)")
    print(f"PDF sin XML con correo (se identifican por su texto): {len(sueltos)}")
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
            actualizadas = 0
            if not args.solo_evidencia:
                actualizadas = await registrar_correos(uow=uow, access=acceso, correos=con_asunto)
        informe: dict[str, Any] = {"errores": []}
        from scripts.automatizar import anotar_correos_de_pdf_sueltos

        por_pdf = 0
        if not args.solo_evidencia:
            por_pdf = await anotar_correos_de_pdf_sueltos(
                uow_factory, usuario, acceso, Path(args.destino), sueltos, informe
            )
        from scripts.automatizar import completar_evidencia

        evidencia = await completar_evidencia(
            uow_factory, settings, usuario, acceso, Path(args.destino)
        )
    finally:
        await db.close()
    print(f"Compras actualizadas: {actualizadas} (por carpeta) + {por_pdf} (por PDF sin XML)")
    print(
        f"Carpeta + correo.txt: {evidencia['completadas']} completadas, "
        f"{evidencia['ya_completas']} ya estaban, {evidencia['sin_archivos']} sin archivos "
        f"(de {evidencia['compras_con_correo']} compras con correo)"
    )
    for error in informe["errores"] + evidencia["errores"]:
        print(f"  error: {error}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Anota el correo de origen en cada compra")
    parser.add_argument("--destino", default="~/Downloads/FacturasProveedores")
    parser.add_argument("--ruc", default="20514831620")
    parser.add_argument("--aplicar", action="store_true", help="Escribe de verdad (si no, simula)")
    parser.add_argument(
        "--solo-evidencia",
        action="store_true",
        help="Solo completa carpeta + correo.txt (no vuelve a anotar el correo en la base)",
    )
    args = parser.parse_args(argv)
    args.destino = str(Path(args.destino).expanduser().resolve())
    return asyncio.run(ejecutar(args))


if __name__ == "__main__":
    sys.exit(main())
