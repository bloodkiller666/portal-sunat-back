"""Publica en la carpeta de red solo las carpetas de año (`AAAA/N. MES/RUC/SERIE-NÚMERO`).

El extractor trabaja en una carpeta local (`--destino`) donde también deja sus apoyos
(`_correos`, `_sin_clasificar`, `_originales_zip`, `registro_correos.jsonl`…). Gerencia solo
necesita ver el orden por año, así que aquí se copia ÚNICAMENTE lo que cuelga de una carpeta de
cuatro dígitos. Es una copia en un solo sentido: nunca borra ni sobrescribe con algo más
antiguo, y un fallo en un archivo se informa sin cortar el resto.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any

_ANIO = re.compile(r"^\d{4}$")


def _hace_falta_copiar(origen: Path, destino: Path) -> bool:
    if not destino.exists():
        return True
    o, d = origen.stat(), destino.stat()
    # El mismo tamaño y un destino igual o más reciente: ya está publicado.
    return o.st_size != d.st_size or o.st_mtime > d.st_mtime + 2


def _ultimo_cambio(estado: os.stat_result) -> float:
    """Cuándo apareció o cambió el archivo en ESTA PC (lo que copia `copy2` conserva la fecha
    original, pero la de creación es la de hoy): el mayor de ambos."""
    return max(estado.st_mtime, getattr(estado, "st_birthtime", 0.0))


def publicar_anios(
    origen: Path,
    red: Path,
    *,
    simular: bool,
    modificados_desde: float | None = None,
    listar: bool = False,
) -> dict[str, Any]:
    """Copia `origen/AAAA/**` a `red/AAAA/**`. Devuelve cuántos archivos copió y los errores.

    Comparar cada archivo con su copia en la red cuesta un viaje por archivo (unos 240 s con
    ~2800 archivos). Con `modificados_desde` (marca de tiempo) solo se miran los archivos
    nuevos o cambiados en esta PC desde entonces; el resto se da por publicado
    (`sin_revisar`). Una pasada sin ese filtro (la «completa») recupera lo que se hubiera
    quedado atrás. Con `listar` el resumen trae `archivos` (lo copiado, para comparar).
    """
    resumen: dict[str, Any] = {"copiados": 0, "ya_estaban": 0, "sin_revisar": 0, "errores": []}
    if listar:
        resumen["archivos"] = []
    if not origen.is_dir():
        return resumen
    for carpeta in sorted(p for p in origen.iterdir() if p.is_dir() and _ANIO.match(p.name)):
        for archivo in sorted(p for p in carpeta.rglob("*") if p.is_file()):
            destino = red / archivo.relative_to(origen)
            try:
                if modificados_desde is not None and (
                    _ultimo_cambio(archivo.stat()) < modificados_desde
                ):
                    resumen["sin_revisar"] += 1
                    continue
                if not _hace_falta_copiar(archivo, destino):
                    resumen["ya_estaban"] += 1
                    continue
                if not simular:
                    destino.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(archivo, destino)
                resumen["copiados"] += 1
                if listar:
                    resumen["archivos"].append(archivo.relative_to(origen).as_posix())
            except OSError as exc:
                resumen["errores"].append(f"{archivo.relative_to(origen).as_posix()}: {exc}")
    return resumen
