"""Apartado «Logs»: las corridas de la automatización diaria y su estado actual.

La automatización corre en una PC; cada corrida deja aquí su resumen (ver `scripts/automatizar.py`)
para poder verlo desde el portal, que está en la web y no puede leer los archivos locales.
"""

from __future__ import annotations

from typing import Any

from app.db.models import AutomatizacionLog, CompanyMember
from app.db.repositories.base import UnitOfWork
from app.services.results import EstadoAutomatizacionOut, LogAutomatizacionOut, LogsPage

LIMITE_POR_DEFECTO = 50
LIMITE_MAXIMO = 200


def _a_salida(log: AutomatizacionLog) -> LogAutomatizacionOut:
    return LogAutomatizacionOut(
        id=log.id,
        created_at=log.created_at,
        modo=log.modo,
        tareas=log.tareas,
        resultado=log.resultado,
        duracion_segundos=log.duracion_segundos,
        novedades=log.novedades,
        errores=log.errores,
        advertencias=log.advertencias,
        detalle=log.detalle,
    )


def _texto(valor: Any) -> str | None:
    return valor if isinstance(valor, str) and valor else None


def _lista(valor: Any) -> list[str]:
    return [x for x in valor if isinstance(x, str)] if isinstance(valor, list) else []


def estado_de(ultima: AutomatizacionLog | None) -> EstadoAutomatizacionOut | None:
    """El estado sale de la foto que guardó la última corrida."""
    if ultima is None:
        return None
    return EstadoAutomatizacionOut(
        ultima_corrida_at=ultima.created_at,
        ultimo_resultado=ultima.resultado,
        ultima_sire_ok=_texto(ultima.estado.get("ultima_sire_ok")),
        ultimo_reintento_sueltos=_texto(ultima.estado.get("ultimo_reintento_sueltos")),
        carpetas_pendientes=_lista(ultima.pendientes.get("carpetas")),
        sueltos_pendientes=_lista(ultima.pendientes.get("sin_clasificar")),
    )


async def listar_logs(
    *,
    uow: UnitOfWork,
    access: CompanyMember,
    resultado: str | None = None,
    limit: int = LIMITE_POR_DEFECTO,
    offset: int = 0,
) -> LogsPage:
    """Corridas de la empresa (cualquier miembro, incluido `lector`) con su estado actual."""
    limite = max(1, min(limit, LIMITE_MAXIMO))
    items, total = await uow.automatizacion_logs.listar(
        access.company_id, resultado=resultado, limit=limite, offset=max(0, offset)
    )
    # El estado es el de la última corrida de todas, no de la última que cumple el filtro.
    if resultado is None and offset == 0 and items:
        ultima: AutomatizacionLog | None = items[0]
    else:
        recientes, _ = await uow.automatizacion_logs.listar(
            access.company_id, resultado=None, limit=1, offset=0
        )
        ultima = recientes[0] if recientes else None
    return LogsPage(items=[_a_salida(x) for x in items], total=total, estado=estado_de(ultima))
