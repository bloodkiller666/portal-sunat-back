"""Mapeo de los códigos de SUNAT a los estados del frontend (punto D).

Estados del frontend: Aprobado, Rechazado, Pendiente, Anulado.

Tabla de decisión (códigos del Manual del Programador de SUNAT):

===================  ==========================  ==================
Código SUNAT         Significado                 Estado frontend
===================  ==========================  ==================
0                    Procesó correctamente       Aprobado
98                   En proceso                  Pendiente
Otro (p. ej. 1033,   El comprobante no existe    Pendiente
errores del CDR)     o aún no fue informado
Cualquier otro       Proceso con errores /       Rechazado
                     comprobante rechazado
===================  ==========================  ==================

"Anulado" no proviene del CDR del comprobante: es el resultado de una
comunicación de baja aceptada, así que se resuelve con `map_cdr_a_estado(...,
es_comunicacion_baja=True)`. La emisión y la baja son de una fase posterior; el
mapeo queda listo y probado desde ahora.
"""

from __future__ import annotations

import re

from app.core.enums import EstadoComprobante
from app.sunat.models import CdrInfo

CODIGO_ACEPTADO = "0"
CODIGO_EN_PROCESO = "98"

_CODIGOS_CON_TABLA = frozenset({CODIGO_ACEPTADO, CODIGO_EN_PROCESO})

# Cuando el código no está en la tabla, el mensaje decide: SUNAT avisa que el
# comprobante todavía no obra en sus registros (no es un rechazo).
_PATRONES_EN_PROCESO = (
    re.compile(r"en\s+proceso", re.IGNORECASE),
    re.compile(r"no\s+(se\s+)?(existe|encontr|ubica)", re.IGNORECASE),
    re.compile(r"no\s+ha\s+sido\s+(enviad|informad|recibid)", re.IGNORECASE),
    re.compile(r"pendiente\s+de", re.IGNORECASE),
)


def map_codigo_a_estado(codigo: str | None, mensaje: str | None = None) -> EstadoComprobante:
    """Traduce un código de SUNAT al estado del frontend."""
    if codigo is None:
        return EstadoComprobante.pendiente

    normalizado = codigo.strip()
    if normalizado == CODIGO_ACEPTADO:
        return EstadoComprobante.aprobado
    if normalizado == CODIGO_EN_PROCESO:
        return EstadoComprobante.pendiente

    if (
        normalizado not in _CODIGOS_CON_TABLA
        and mensaje
        and any(patron.search(mensaje) for patron in _PATRONES_EN_PROCESO)
    ):
        return EstadoComprobante.pendiente

    return EstadoComprobante.rechazado


def map_cdr_a_estado(cdr: CdrInfo, *, es_comunicacion_baja: bool = False) -> EstadoComprobante:
    """Traduce un CDR al estado del frontend."""
    if es_comunicacion_baja:
        return EstadoComprobante.anulado if cdr.aceptado else EstadoComprobante.rechazado
    return map_codigo_a_estado(cdr.codigo, cdr.descripcion)


def map_baja_a_estado(cdr: CdrInfo) -> EstadoComprobante:
    """Baja (comunicación de baja) aceptada -> comprobante anulado."""
    return map_cdr_a_estado(cdr, es_comunicacion_baja=True)
