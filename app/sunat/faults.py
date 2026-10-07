"""Clasificación de los SOAP Fault de SUNAT.

Importa distinguir tres situaciones:

1. **Credenciales inválidas** — el usuario/clave SOL no autenticó.
2. **Comprobante aún no disponible** — SUNAT todavía no tiene el comprobante
   (sigue pendiente de envío); no es un error del portal.
3. **Otro error de dominio** — el comprobante existe pero fue rechazado, o la
   trama tiene un problema.

Los códigos provienen del Manual del Programador (Anexo de códigos de error).
El mapeo se concentra aquí para poder ajustarlo sin tocar los servicios.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PATRONES_AUTH = (
    re.compile(r"no\s+autorizad", re.IGNORECASE),  # "El Usuario no autorizado"
    re.compile(r"no\s+est[áa]\s+autorizad", re.IGNORECASE),
    re.compile(r"not\s+authoriz", re.IGNORECASE),
    re.compile(r"unauthoriz", re.IGNORECASE),
    re.compile(r"\bcredenciales\b", re.IGNORECASE),
    re.compile(r"usuario\s+o\s+clave", re.IGNORECASE),
    re.compile(
        r"(clave|contrase[ñn]a|password|usuario)\b[^.]{0,40}?"
        r"(incorrect[ao]?|inv[áa]lid[ao]|no\s+v[áa]lid[ao]|no\s+activ[ao]|no\s+existe)",
        re.IGNORECASE,
    ),
    re.compile(r"authentication\s+failed|failed\s+authentication", re.IGNORECASE),
)

# Códigos del Anexo de errores: 0104 clave incorrecta, 0105 usuario no activo,
# 0156/0157 relacionados a la firma del XML, 1033 comprobante no existe.
CODIGOS_AUTENTICACION = frozenset({"0104", "0105", "401", "403"})
CODIGOS_NO_DISPONIBLE = frozenset({"1033"})

_PATRONES_NO_DISPONIBLE = (
    re.compile(r"no\s+(se\s+)?(existe|encontr)", re.IGNORECASE),
    re.compile(r"en\s+proceso", re.IGNORECASE),
    re.compile(r"no\s+ha\s+sido\s+(enviad|informad)", re.IGNORECASE),
)


@dataclass(frozen=True, slots=True)
class ClasificacionFault:
    """Resultado de clasificar un fault."""

    tipo: str  # "autenticacion" | "no_disponible" | "dominio"
    codigo: str | None
    mensaje: str

    @property
    def es_autenticacion(self) -> bool:
        return self.tipo == "autenticacion"

    @property
    def es_no_disponible(self) -> bool:
        return self.tipo == "no_disponible"


def _normalizar_codigo(codigo: str | None) -> str | None:
    if not codigo:
        return None
    limpio = codigo.strip()
    # SUNAT devuelve "soap-env:Client" en el código del fault; el código de negocio
    # viene dentro del mensaje o del detalle.
    if ":" in limpio:
        limpio = limpio.split(":")[-1]
    return limpio or None


def _codigo_en_mensaje(mensaje: str) -> str | None:
    coincidencia = re.search(r"\b(\d{4})\b", mensaje)
    return coincidencia.group(1) if coincidencia else None


def clasificar_fault(codigo: str | None, mensaje: str | None) -> ClasificacionFault:
    """Clasifica un fault de SUNAT a partir de su código y mensaje."""
    texto = (mensaje or "").strip()
    codigo_normalizado = _normalizar_codigo(codigo)
    codigo_negocio = (
        codigo_normalizado
        if codigo_normalizado
        in {
            *CODIGOS_AUTENTICACION,
            *CODIGOS_NO_DISPONIBLE,
        }
        else _codigo_en_mensaje(texto)
    )

    if codigo_negocio in CODIGOS_AUTENTICACION or any(p.search(texto) for p in _PATRONES_AUTH):
        return ClasificacionFault(
            tipo="autenticacion",
            codigo=codigo_negocio or codigo_normalizado,
            mensaje=texto or "SUNAT rechazó las credenciales",
        )

    if codigo_negocio in CODIGOS_NO_DISPONIBLE or any(
        p.search(texto) for p in _PATRONES_NO_DISPONIBLE
    ):
        return ClasificacionFault(
            tipo="no_disponible",
            codigo=codigo_negocio or codigo_normalizado,
            mensaje=texto or "SUNAT aún no tiene el comprobante",
        )

    return ClasificacionFault(
        tipo="dominio",
        codigo=codigo_negocio or codigo_normalizado,
        mensaje=texto or "SUNAT devolvió un error",
    )
