"""Nombres de los meses para las carpetas (`2026/10. OCTUBRE`) en la PC y en el bucket.

Una carpeta `10` confunde a quien no sabe si es mes o día, y `OCTUBRE` solo se ordena
alfabéticamente (ABRIL, AGOSTO, DICIEMBRE…). `10. OCTUBRE` dice el mes y se ordena como el año:
el explorador de Windows y el de R2 las muestran en orden. Los nombres son los que muestra el
portal (con «Setiembre», como se dice en Perú) y el frontend los repite en `lib/calendario.ts`: si
cambian aquí, cambian allá, porque la carpeta que arma el navegador debe coincidir con la que dejan
el extractor y el backend.

Las carpetas guardadas antes (`2026/OCTUBRE`, `2026/10`) se siguen encontrando con
`variantes_de_mes`.
"""

from __future__ import annotations

import re
from datetime import date

NOMBRES_MES = (
    "ENERO",
    "FEBRERO",
    "MARZO",
    "ABRIL",
    "MAYO",
    "JUNIO",
    "JULIO",
    "AGOSTO",
    "SETIEMBRE",
    "OCTUBRE",
    "NOVIEMBRE",
    "DICIEMBRE",
)

_CARPETA_DE_MES = re.compile(r"^(\d{1,2})\. ([A-ZÁÉÍÓÚÑ]+)$")


def nombre_mes(mes: int) -> str:
    """`10` → `OCTUBRE`."""
    return NOMBRES_MES[mes - 1]


def carpeta_de_mes(mes: int) -> str:
    """`10` → `10. OCTUBRE`: el nombre de la carpeta del mes (sin cero a la izquierda)."""
    return f"{mes}. {NOMBRES_MES[mes - 1]}"


def numero_de_mes(parte: str) -> int | None:
    """El mes (1-12) de una carpeta en cualquiera de sus formas: `10. OCTUBRE`, `OCTUBRE` o `10`."""
    texto = parte.strip().upper()
    coincidencia = _CARPETA_DE_MES.match(texto)
    if coincidencia:
        numero, nombre = int(coincidencia.group(1)), coincidencia.group(2)
        return numero if 1 <= numero <= 12 and NOMBRES_MES[numero - 1] == nombre else None
    if texto in NOMBRES_MES:
        return NOMBRES_MES.index(texto) + 1
    if re.fullmatch(r"0?[1-9]|1[0-2]", texto):
        return int(texto)
    return None


def es_carpeta_de_mes(parte: str) -> bool:
    """¿Es exactamente `10. OCTUBRE`? (esa forma conserva su espacio en la clave de R2)."""
    return numero_de_mes(parte) is not None and _CARPETA_DE_MES.match(parte.strip()) is not None


def variantes_de_mes(parte: str) -> list[str]:
    """Las formas con que pudo guardarse el mes, la actual primero: `10. OCTUBRE`, `OCTUBRE`, `10`.

    Si `parte` no es un mes (`sin-fecha`) se devuelve tal cual.
    """
    mes = numero_de_mes(parte)
    if mes is None:
        return [parte]
    return [carpeta_de_mes(mes), nombre_mes(mes), f"{mes:02d}"]


def mes_numerico(nombre: str) -> str | None:
    """`OCTUBRE` → `10` (la carpeta con que se guardaba al principio); `None` si no es un nombre."""
    texto = nombre.strip().upper()
    return f"{NOMBRES_MES.index(texto) + 1:02d}" if texto in NOMBRES_MES else None


def periodo_carpeta(fecha: date) -> str:
    """`2026-10. OCTUBRE`: carpetas de auditoría por mes (`_correos`, `_sin_clasificar`)."""
    return f"{fecha.year:04d}-{carpeta_de_mes(fecha.month)}"
