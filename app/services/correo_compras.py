"""Anota en cada compra de qué correo llegó (asunto, remitente y fecha de recepción).

El extractor del buzón de facturas deja, por cada correo, un registro con sus archivos
(`_correos/AAAA-MM/….json`); la carpeta de cada archivo (`AAAA/MM/RUC/SERIE-NÚMERO`) identifica
la compra. Aquí solo se aplica ese dato a la base: sin leer el disco ni Outlook.

Si la misma factura llegó en varios correos se conserva el primero (el de fecha más antigua), de
modo que repetir la carga o reprocesar el buzón nunca cambia lo ya anotado.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from app.db.models import CompanyMember
from app.db.repositories.base import UnitOfWork

_CARPETA_DE_COMPRA = re.compile(r"^\d{4}/\d{2}/(\d{11})/([A-Z0-9]{4})-(\d{1,8})$", re.IGNORECASE)
MAX_ASUNTO = 500
MAX_REMITENTE = 300


@dataclass(frozen=True)
class CorreoDeCompra:
    """El correo en que llegó una compra, identificada por su proveedor y serie-número."""

    ruc_emisor: str
    serie: str
    correlativo: int
    asunto: str
    remitente: str | None
    recibido_at: datetime


def clave_de_carpeta(carpeta: str) -> tuple[str, str, int] | None:
    """`AAAA/MM/RUC/SERIE-NÚMERO` → `(ruc, serie, correlativo)`; `None` si no es una compra."""
    coincidencia = _CARPETA_DE_COMPRA.match(carpeta.replace("\\", "/").strip("/"))
    if not coincidencia:
        return None
    ruc, serie, numero = coincidencia.groups()
    return ruc, serie.upper(), int(numero)


def _limpio(texto: str | None, maximo: int) -> str:
    return " ".join((texto or "").split())[:maximo]


async def registrar_correos(
    *, uow: UnitOfWork, access: CompanyMember, correos: list[CorreoDeCompra]
) -> int:
    """Aplica los correos a sus compras. Devuelve cuántas compras se actualizaron.

    Un correo sin asunto no se anota (no diría nada). Lo que no coincide con ninguna compra de
    la empresa simplemente no actualiza nada.
    """
    actualizadas = 0
    for correo in correos:
        asunto = _limpio(correo.asunto, MAX_ASUNTO)
        if not asunto:
            continue
        actualizadas += await uow.comprobantes.registrar_correo(
            access.company_id,
            ruc_emisor=correo.ruc_emisor,
            serie=correo.serie,
            correlativo=correo.correlativo,
            asunto=asunto,
            remitente=_limpio(correo.remitente, MAX_REMITENTE) or None,
            recibido_at=correo.recibido_at,
        )
    return actualizadas
