"""Carpeta local de respaldo: `Descargas/AAAA/MM/SERIE-NÚMERO/RUC`.

El navegador guarda ahí el XML y el CDR (ver el frontend). Este módulo solo calcula esa ruta y,
cuando el backend corre en la MISMA PC del usuario, pide al sistema operativo abrirla. Por
seguridad nunca recibe una ruta del cliente: la deriva del comprobante y la limita a la carpeta
base configurada (`LOCAL_DESCARGAS_DIR`, por defecto `~/Downloads`).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from uuid import UUID

from app.core.config import Settings
from app.core.enums import OrigenComprobante
from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.security import AuthenticatedUser
from app.db.models import Comprobante
from app.db.repositories.base import UnitOfWork
from app.services.companies import require_membership

logger = get_logger(__name__)

_NO_PERMITIDO = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def _limpio(texto: str, vacio: str) -> str:
    return _NO_PERMITIDO.sub("-", texto).strip(" .") or vacio


def partes_de_carpeta(comprobante: Comprobante) -> list[str]:
    """Año, mes, serie-número y RUC de la contraparte (cliente en ventas, proveedor en compras)."""
    fecha = comprobante.fecha_emision
    anio, mes = (f"{fecha.year:04d}", f"{fecha.month:02d}") if fecha else ("sin-fecha", "sin-fecha")
    es_compra = comprobante.origen is OrigenComprobante.recibido
    ruc = comprobante.ruc_emisor if es_compra else comprobante.ruc_receptor
    return [
        anio,
        mes,
        _limpio(f"{comprobante.serie}-{comprobante.numero}", "sin-numero"),
        _limpio(ruc or "", "sin-ruc"),
    ]


def carpeta_base(settings: Settings) -> Path:
    return (settings.local_descargas_dir or Path.home() / "Downloads").resolve()


def _abrir_en_explorador(ruta: Path) -> None:
    if sys.platform == "win32":
        os.startfile(ruta)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(ruta)])
    else:
        subprocess.Popen(["xdg-open", str(ruta)])


async def abrir_carpeta(
    *, uow: UnitOfWork, settings: Settings, user: AuthenticatedUser, comprobante_id: UUID
) -> str:
    """Abre en el explorador la carpeta del comprobante y devuelve su ruta completa."""
    if not settings.local_abrir_carpeta:
        raise ConflictError(
            "Abrir carpetas solo funciona si el backend corre en tu PC "
            "(LOCAL_ABRIR_CARPETA=true en su .env).",
            details={"motivo": "deshabilitado"},
        )
    comprobante = await uow.comprobantes.get(comprobante_id)
    if comprobante is None:
        raise NotFoundError("El comprobante no existe o no pertenece a sus empresas")
    await require_membership(uow, user, comprobante.company_id)

    base = carpeta_base(settings)
    ruta = base.joinpath(*partes_de_carpeta(comprobante)).resolve()
    if not ruta.is_relative_to(base):  # defensa en profundidad: los nombres ya están limpios
        raise NotFoundError("La carpeta del comprobante no es válida")
    if not ruta.is_dir():
        raise NotFoundError(
            "Esa carpeta aún no existe en la PC: usa «Guardar en mi PC» primero.",
            details={"ruta": str(ruta)},
        )
    _abrir_en_explorador(ruta)
    logger.info("Carpeta local abierta: %s", ruta)
    return str(ruta)
