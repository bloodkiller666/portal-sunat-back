"""Nombres de los meses para las carpetas (`2026/OCTUBRE`) en la PC y en el bucket.

Una carpeta `10` confunde a quien no sabe si es mes o día; `OCTUBRE` no. Los nombres son los
mismos que muestra el portal (con «Setiembre», como se dice en Perú) y el frontend los repite en
`lib/calendario.ts`: si cambian aquí, cambian allá, porque la carpeta que arma el navegador debe
coincidir con la que dejan el extractor y el backend.
"""

from __future__ import annotations

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


def nombre_mes(mes: int) -> str:
    """`10` → `OCTUBRE`."""
    return NOMBRES_MES[mes - 1]


def mes_numerico(nombre: str) -> str | None:
    """`OCTUBRE` → `10` (la carpeta con que se guardaba antes), o `None` si no es un mes."""
    try:
        return f"{NOMBRES_MES.index(nombre.strip().upper()) + 1:02d}"
    except ValueError:
        return None


def periodo_carpeta(fecha: date) -> str:
    """`2026-OCTUBRE`: carpetas de auditoría agrupadas por mes (`_correos`, `_sin_clasificar`)."""
    return f"{fecha.year:04d}-{nombre_mes(fecha.month)}"
