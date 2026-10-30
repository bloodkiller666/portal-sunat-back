"""Anota en cada compra de qué correo llegó (asunto, remitente y fecha de recepción).

El extractor del buzón de facturas deja, por cada correo, un registro con sus archivos
(`_correos/AAAA-MES/….json`); la carpeta de cada archivo (`AAAA/MES/RUC/SERIE-NÚMERO`) identifica
la compra. Aquí solo se aplica ese dato a la base: sin leer el disco ni Outlook.

Si la misma factura llegó en varios correos se conserva el primero (el de fecha más antigua), de
modo que repetir la carga o reprocesar el buzón nunca cambia lo ya anotado.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.db.models import Archivo, CompanyMember, Comprobante
from app.db.repositories.base import UnitOfWork

# El mes es `10. OCTUBRE` o, en carpetas anteriores, el nombre (`OCTUBRE`) o el número (`10`).
_MES = r"(?:\d{1,2}\. [A-ZÁÉÍÓÚ]{4,10}|\d{2}|[A-ZÁÉÍÓÚ]{4,10})"
_CARPETA_DE_COMPRA = re.compile(
    rf"^\d{{4}}/{_MES}/(\d{{11}})/([A-Z0-9]{{4}})-(\d{{1,8}})$", re.IGNORECASE
)
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
    """`AAAA/MES/RUC/SERIE-NÚMERO` → `(ruc, serie, correlativo)`; `None` si no es una compra."""
    coincidencia = _CARPETA_DE_COMPRA.match(carpeta.replace("\\", "/").strip("/"))
    if not coincidencia:
        return None
    ruc, serie, numero = coincidencia.groups()
    return ruc, serie.upper(), int(numero)


def _limpio(texto: str | None, maximo: int) -> str:
    return " ".join((texto or "").split())[:maximo]


NOMBRE_CORREO_TXT = "correo.txt"
_LIMA = timezone(timedelta(hours=-5))  # Perú no usa horario de verano


def _a_hora_de_lima(momento: datetime) -> datetime:
    """La base guarda la recepción en UTC; el buzón y el usuario la ven en hora de Lima."""
    if momento.tzinfo is None:  # naive: ya viene en hora local
        return momento
    return momento.astimezone(_LIMA)


def texto_correo_de_compra(
    comprobante: Comprobante,
    archivos: list[Archivo],
    *,
    mensaje_original: str | None = None,
) -> str | None:
    """`correo.txt` de una compra, armado con lo que Supabase guarda de su correo.

    Es la constancia, junto a los archivos, de que la factura llegó por el buzón: asunto,
    remitente, fecha de recepción y los adjuntos con su SHA-256 (el mismo que está en `archivos`).
    El cuerpo no se guarda en la base; si se respaldó el correo completo (`.msg`), se indica dónde.
    Devuelve `None` si la compra no tiene correo anotado.
    """
    if not comprobante.correo_asunto or comprobante.correo_recibido_at is None:
        return None
    recibido = _a_hora_de_lima(comprobante.correo_recibido_at)
    lineas = [
        f"Asunto:    {comprobante.correo_asunto}",
        f"De:        {comprobante.correo_remitente or '(desconocido)'}",
        f"Recibido:  {recibido:%Y-%m-%d %H:%M:%S} (hora de Lima)",
        "Buzón:     recepcionfacturas",
        "",
        f"Comprobante: {comprobante.serie}-{comprobante.numero} de {comprobante.ruc_emisor}",
        "",
        "Archivos de este comprobante (huella SHA-256 registrada en el portal):",
        *[
            f"  - {a.nombre} ({a.size_bytes} bytes, sha256 {(a.hash_sha256 or '')[:16]}…)"
            for a in archivos
        ],
    ]
    if mensaje_original:
        lineas += ["", f"Correo completo (.msg): {mensaje_original}"]
    return "\n".join(lineas) + "\n"


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
