"""Pasa las carpetas de la PC al mes con número y nombre (`2026/10. OCTUBRE`).

    2026/OCTUBRE/RUC/FF01-1                  a  2026/10. OCTUBRE/RUC/FF01-1
    _correos/2026-OCTUBRE/correo.msg         a  _correos/2026-10. OCTUBRE/correo.msg
    _otras_empresas/RUC/2026/OCTUBRE/...     a  _otras_empresas/RUC/2026/10. OCTUBRE/...

Recorre las dos carpetas de la PC (`LOCAL_PROVEEDORES_DIR` y `LOCAL_DESCARGAS_DIR`). En la de
compras también cambia las carpetas de auditoría (`_correos`, `_sin_clasificar`, `_originales_zip`)
y corrige las rutas que citan los `.json`, `.jsonl` y `correo*.txt` (el `.msg` de cada correo, la
carpeta de cada factura en el índice…), para que nada quede apuntando a la carpeta de antes.

Por defecto solo **simula**. Con `--aplicar` renombra; si la carpeta nueva ya existe, mueve lo de la
vieja dentro de ella sin pisar nada. Lo que choque, o esté en uso por otro programa (el Explorador
o un editor con un archivo abierto), se deja donde estaba y se informa: se cierra y se vuelve a
correr. Es idempotente: lo que ya está como `10. OCTUBRE` no se toca.

    uv run python -m scripts.meses_en_carpetas_locales             # plan
    uv run python -m scripts.meses_en_carpetas_locales --aplicar
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

from app.core.config import Settings
from app.core.meses import NOMBRES_MES, carpeta_de_mes, numero_de_mes

_AUDITORIA = ("_correos", "_sin_clasificar", "_originales_zip")
_ANIO = re.compile(r"^\d{4}$")
_PERIODO = re.compile(r"^(\d{4})-(.+)$")
_MESES = "|".join(NOMBRES_MES)
# Rutas escritas en los `.json`, `.jsonl` y `correo*.txt`: `2026/OCTUBRE/` y `2026-OCTUBRE/`.
_RUTA_ANUAL = re.compile(rf"(?<!\d)(\d{{4}})/({_MESES})/(?=\d{{11}}/)")
_RUTA_PERIODO = re.compile(rf"(?<!\d)(\d{{4}})-({_MESES})(?=[/\\\"'\s,]|$)")


def carpeta_nueva(nombre: str) -> str | None:
    """`OCTUBRE` o `10` → `10. OCTUBRE`; `None` si no es un mes o ya está bien."""
    mes = numero_de_mes(nombre)
    if mes is None or nombre == carpeta_de_mes(mes):
        return None
    return carpeta_de_mes(mes)


def periodo_nuevo(nombre: str) -> str | None:
    """`2026-OCTUBRE` → `2026-10. OCTUBRE`; `None` si no es un periodo o ya está bien."""
    m = _PERIODO.match(nombre)
    nuevo = carpeta_nueva(m.group(2)) if m else None
    return f"{m.group(1)}-{nuevo}" if m and nuevo else None


def corregir_texto(texto: str) -> str:
    """El texto con las rutas de mes en letras llevadas al formato `10. OCTUBRE`."""

    def anual(m: re.Match[str]) -> str:
        return f"{m.group(1)}/{carpeta_de_mes(NOMBRES_MES.index(m.group(2)) + 1)}/"

    def periodo(m: re.Match[str]) -> str:
        return f"{m.group(1)}-{carpeta_de_mes(NOMBRES_MES.index(m.group(2)) + 1)}"

    return _RUTA_PERIODO.sub(periodo, _RUTA_ANUAL.sub(anual, texto))


def _renombrar(origen: Path, destino: Path, aplicar: bool, conflictos: list[str]) -> int:
    """Renombra `origen` a `destino`; si ya existe, funde sin pisar. Devuelve cuántas carpetas."""
    if not aplicar:
        return 1
    if not destino.exists():
        try:
            origen.rename(destino)
            return 1
        except PermissionError:
            # Un programa tiene abierta la carpeta (el Explorador, un editor con un archivo de
            # adentro): se crea la nueva y se mueve lo que no esté en uso, sin forzar nada.
            destino.mkdir()
    for hijo in sorted(origen.iterdir()):
        objetivo = destino / hijo.name
        try:
            if not objetivo.exists():
                shutil.move(str(hijo), str(objetivo))
            elif hijo.is_dir() and objetivo.is_dir():
                _renombrar(hijo, objetivo, True, conflictos)
            else:
                conflictos.append(f"ya existía: {hijo}")
        except PermissionError:
            conflictos.append(f"en uso por otro programa: {hijo}")
    if not any(origen.iterdir()):
        origen.rmdir()
    return 1


def _meses_de(carpeta: Path, aplicar: bool, conflictos: list[str]) -> int:
    """`carpeta/AAAA/MES` → `carpeta/AAAA/N. MES`."""
    cuenta = 0
    if not carpeta.is_dir():
        return 0
    for anio in sorted(p for p in carpeta.iterdir() if p.is_dir() and _ANIO.match(p.name)):
        for mes in sorted(p for p in anio.iterdir() if p.is_dir()):
            nuevo = carpeta_nueva(mes.name)
            if nuevo:
                cuenta += _renombrar(mes, anio / nuevo, aplicar, conflictos)
    return cuenta


def _periodos_de(carpeta: Path, aplicar: bool, conflictos: list[str]) -> int:
    """`carpeta/AAAA-MES` → `carpeta/AAAA-N. MES` (las carpetas de auditoría)."""
    cuenta = 0
    if not carpeta.is_dir():
        return 0
    for periodo in sorted(p for p in carpeta.iterdir() if p.is_dir()):
        nuevo = periodo_nuevo(periodo.name)
        if nuevo:
            cuenta += _renombrar(periodo, carpeta / nuevo, aplicar, conflictos)
    return cuenta


def _textos_a_corregir(base: Path) -> list[Path]:
    rutas = [base / "indice_comprobantes.jsonl", base / "informe.json"]
    rutas.append(base / "_automatizacion" / "pendientes_importar.json")
    rutas.extend((base / "_correos").rglob("*.json"))
    rutas.extend(p for p in base.glob("[0-9][0-9][0-9][0-9]/*/*/*/correo*.txt"))
    rutas.extend(base.glob("_sin_clasificar/*/*/correo*.txt"))
    rutas.extend(base.glob("_otras_empresas/*/*/*/*/*/correo*.txt"))
    return [r for r in rutas if r.is_file()]


def _corregir_textos(base: Path, aplicar: bool) -> int:
    corregidos = 0
    for ruta in _textos_a_corregir(base):
        original = ruta.read_text(encoding="utf-8", errors="surrogateescape")
        nuevo = corregir_texto(original)
        if nuevo != original:
            corregidos += 1
            if aplicar:
                ruta.write_text(nuevo, encoding="utf-8", errors="surrogateescape")
    return corregidos


def migrar_compras(base: Path, aplicar: bool, conflictos: list[str]) -> dict[str, int]:
    """Todo lo de la carpeta de compras. Primero se renombran las carpetas y luego los textos."""
    resumen = {
        "carpetas_de_mes": _meses_de(base, aplicar, conflictos),
        "otras_empresas": 0,
        "auditoria": 0,
    }
    otras = base / "_otras_empresas"
    if otras.is_dir():
        for ruc in sorted(p for p in otras.iterdir() if p.is_dir()):
            resumen["otras_empresas"] += _meses_de(ruc, aplicar, conflictos)
    for nombre in _AUDITORIA:
        resumen["auditoria"] += _periodos_de(base / nombre, aplicar, conflictos)
    resumen["textos"] = _corregir_textos(base, aplicar)
    return resumen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Meses como «10. OCTUBRE» en las carpetas locales")
    parser.add_argument("--aplicar", action="store_true", help="Renombra de verdad (si no, simula)")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args(argv)

    settings = Settings(_env_file=args.env_file)
    compras = (
        settings.local_proveedores_dir or Path.home() / "Downloads" / "FacturasProveedores"
    ).resolve()
    ventas = (settings.local_descargas_dir or Path.home() / "Downloads").resolve()
    conflictos: list[str] = []
    print(f"{'APLICANDO' if args.aplicar else 'SIMULACIÓN'}")
    print(f"Compras: {compras}")
    for nombre, cuenta in migrar_compras(compras, args.aplicar, conflictos).items():
        print(f"  {nombre}: {cuenta}")
    if ventas != compras:
        print(f"Ventas: {ventas}")
        print(f"  carpetas_de_mes: {_meses_de(ventas, args.aplicar, conflictos)}")
    for conflicto in conflictos:
        print(f"  ! se dejó donde estaba ({conflicto})")
    if not args.aplicar:
        print("Simulación: nada se renombró. Usa --aplicar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
