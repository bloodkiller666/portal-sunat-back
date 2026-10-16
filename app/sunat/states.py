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
# SUNAT responde con este código cuando el comprobante todavía no obra en sus
# registros: no es un rechazo, es que aún no lo ha recibido. `0011` es el que
# devuelve hoy `billConsultService.getStatus` ("El comprobante de pago
# electrónico no existe"); `1033` aparece en otras variantes/documentación.
CODIGO_NO_INFORMADO = "1033"
CODIGO_NO_EXISTE = "0011"
# `getStatusCdr` devuelve este código cuando el comprobante no tiene ticket de
# consulta asociado (respuesta observada en producción).
CODIGO_TICKET_NO_EXISTE = "0127"
# «El comprobante de pago electrónico no pertenece a GEM»: el comprobante no fue
# enviado por el canal que este servicio consulta (típico de lo emitido por un
# OSE/PSE u otro sistema). SUNAT no lo está rechazando: simplemente esta consulta
# no puede resolverlo, así que no debe cambiar su estado (observado en producción
# con una factura que SUNAT sí lista en SIRE).
CODIGO_FUERA_DE_ALCANCE = "0013"

# Códigos que, sin importar el mensaje, significan "todavía no disponible".
_CODIGOS_PENDIENTES = frozenset(
    {
        CODIGO_EN_PROCESO,
        CODIGO_NO_INFORMADO,
        CODIGO_NO_EXISTE,
        CODIGO_TICKET_NO_EXISTE,
        CODIGO_FUERA_DE_ALCANCE,
    }
)

# Cuando el código no está en la tabla, el mensaje decide: SUNAT avisa que el
# comprobante todavía no obra en sus registros (no es un rechazo).
_PATRONES_EN_PROCESO = (
    re.compile(r"en\s+proceso", re.IGNORECASE),
    re.compile(r"no\s+(se\s+)?(existe|encontr|ubica|registra)", re.IGNORECASE),
    re.compile(r"sin\s+registro", re.IGNORECASE),
    re.compile(r"no\s+hay\s+registro", re.IGNORECASE),
    re.compile(r"no\s+ha\s+sido\s+(enviad|informad|recibid|remitid)", re.IGNORECASE),
    re.compile(r"no\s+informad", re.IGNORECASE),
    re.compile(r"no\s+remitid", re.IGNORECASE),
    re.compile(r"a[uú]n\s+no", re.IGNORECASE),
    re.compile(r"pendiente\s+de", re.IGNORECASE),
    re.compile(r"por\s+informar", re.IGNORECASE),
)


def map_codigo_a_estado(codigo: str | None, mensaje: str | None = None) -> EstadoComprobante:
    """Traduce un código de SUNAT al estado del frontend.

    Reglas, en orden: código aceptado -> Aprobado; código/mensaje que indica que
    SUNAT todavía no tiene el comprobante -> Pendiente; cualquier otra cosa ->
    Rechazado.
    """
    if codigo is None:
        return EstadoComprobante.pendiente

    normalizado = codigo.strip()
    if not normalizado:
        return EstadoComprobante.pendiente
    if normalizado == CODIGO_ACEPTADO:
        return EstadoComprobante.aprobado
    if normalizado in _CODIGOS_PENDIENTES:
        return EstadoComprobante.pendiente

    if mensaje and any(patron.search(mensaje) for patron in _PATRONES_EN_PROCESO):
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
