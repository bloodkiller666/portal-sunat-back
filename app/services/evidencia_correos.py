"""Deja en la carpeta local de cada compra la constancia de que llegó por el correo.

Para cada compra con correo anotado (la columna «Correo» del portal, guardada en Supabase) y con al
menos un archivo (XML o PDF), asegura en `LOCAL_PROVEEDORES_DIR/AAAA/MES/RUC/SERIE-NÚMERO/`:

* sus archivos del bucket (copia solo los que falten), y
* un `correo.txt` con asunto, remitente, fecha de recepción y la huella SHA-256 de cada adjunto.

Es idempotente: una carpeta que ya tiene su `correo.txt` (por ejemplo el del extractor, que además
trae el cuerpo) no se toca. Un fallo en una compra se informa y no frena a las demás.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from app.core.config import Settings
from app.core.enums import OrigenComprobante
from app.core.logging import get_logger
from app.db.models import Comprobante
from app.db.repositories.base import FiltrosListado
from app.services.carpeta_local import (
    carpetas_base,
    correo_txt_del_portal_sin_msg,
    partes_del_comprobante,
    restaurar_carpeta,
)
from app.services.comprobantes import MAX_COMPROBANTES_PAGINA
from app.services.correo_compras import NOMBRE_CORREO_TXT
from app.storage.base import StorageProvider

logger = get_logger(__name__)


_LIMA = timezone(timedelta(hours=-5))
Clave = tuple[str, datetime]


def clave_de_correo(asunto: str, recibido: datetime) -> Clave:
    """Identifica un correo por su asunto (como lo guarda la base) y su hora de recepción en UTC."""
    if recibido.tzinfo is None:  # el extractor guarda la hora local de Lima, sin zona
        recibido = recibido.replace(tzinfo=_LIMA)
    return " ".join(asunto.split())[:500], recibido.astimezone(UTC)


def indice_mensajes(destino: Path) -> dict[Clave, str]:
    """Correos con su `.msg` respaldado: `(asunto, recibido UTC)` → ruta relativa del `.msg`."""
    indice: dict[Clave, str] = {}
    for ruta in sorted((destino / "_correos").rglob("*.json")):
        try:
            datos = json.loads(ruta.read_text(encoding="utf-8"))
            mensaje = (datos.get("mensaje_original") or {}).get("ruta")
            recibido = datetime.fromisoformat(datos["recibido"])
            clave = clave_de_correo(str(datos.get("asunto") or ""), recibido)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
        if mensaje and (destino / mensaje).is_file():
            indice[clave] = str(mensaje)
    return indice


async def _compras_con_correo(
    uow_factory: Callable[..., Any], claims: dict[str, Any], company_id: UUID
) -> list[Comprobante]:
    """Todas las compras de la empresa que tienen un correo anotado (páginas de 100)."""
    encontradas: list[Comprobante] = []
    offset = 0
    filtros = FiltrosListado(origen=OrigenComprobante.recibido)
    while True:
        async with uow_factory(claims) as uow:
            pagina = await uow.comprobantes.list_for_company(
                company_id, filtros, limit=MAX_COMPROBANTES_PAGINA, offset=offset
            )
        encontradas += [c for c in pagina.items if c.correo_asunto and c.correo_recibido_at]
        if len(pagina.items) < MAX_COMPROBANTES_PAGINA:
            return encontradas
        offset += MAX_COMPROBANTES_PAGINA


async def _completar_una(
    uow_factory: Callable[..., Any],
    claims: dict[str, Any],
    settings: Settings,
    compra: Comprobante,
    storage_provider: StorageProvider | None,
    mensaje_original: str | None,
) -> bool:
    async with uow_factory(claims) as uow:
        carpeta = await restaurar_carpeta(
            uow=uow,
            settings=settings,
            comprobante=compra,
            storage_provider=storage_provider,
            mensaje_original=mensaje_original,
        )
    return carpeta is not None


async def completar_evidencia_de_correos(
    *,
    uow_factory: Callable[..., Any],
    claims: dict[str, Any],
    company_id: UUID,
    settings: Settings,
    storage_provider: StorageProvider | None = None,
    simular: bool = False,
    reintentar: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Completa la carpeta y el `correo.txt` de las compras con correo que aún no lo tienen.

    `reintentar(funcion)` (opcional) envuelve cada compra para repetirla si se corta la conexión.
    """
    informe: dict[str, Any] = {
        "compras_con_correo": 0,
        "ya_completas": 0,
        "pendientes": 0,
        "completadas": 0,
        "sin_archivos": 0,
        "errores": [],
    }
    base = carpetas_base(settings, "recibido")[0]
    compras = await _compras_con_correo(uow_factory, claims, company_id)
    informe["compras_con_correo"] = len(compras)
    mensajes = await asyncio.to_thread(indice_mensajes, base)

    def _mensaje(compra: Comprobante) -> str | None:
        if not compra.correo_asunto or compra.correo_recibido_at is None:
            return None
        return mensajes.get(clave_de_correo(compra.correo_asunto, compra.correo_recibido_at))

    pendientes: list[Comprobante] = []
    for compra in compras:
        txt = base.joinpath(*partes_del_comprobante(compra)) / NOMBRE_CORREO_TXT
        # Pendiente: no hay correo.txt, o es del portal y ahora se conoce el .msg del correo.
        if not txt.exists() or (_mensaje(compra) and correo_txt_del_portal_sin_msg(txt)):
            pendientes.append(compra)
        else:
            informe["ya_completas"] += 1
    informe["pendientes"] = len(pendientes)
    if simular:
        return informe

    for compra in pendientes:
        hacer = lambda c=compra: _completar_una(  # noqa: E731 - se repite entero si falla
            uow_factory, claims, settings, c, storage_provider, _mensaje(c)
        )
        try:
            hecho = await (reintentar(hacer) if reintentar else hacer())
        except Exception as exc:
            informe["errores"].append(f"{compra.serie}-{compra.numero}: {type(exc).__name__}")
            continue
        informe["completadas" if hecho else "sin_archivos"] += 1
        await asyncio.sleep(0)  # cede el turno: son cientos de compras
    return informe
