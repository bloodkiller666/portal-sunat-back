"""Apartado «Logs»: las corridas de la automatización diaria y su estado actual.

La automatización corre en una PC; cada corrida deja aquí su resumen (ver `scripts/automatizar.py`)
para poder verlo desde el portal, que está en la web y no puede leer los archivos locales.
"""

from __future__ import annotations

from typing import Any

from app.core.enums import RolEmpresa
from app.core.logging import get_logger
from app.core.redaction import redact
from app.db.models import AutomatizacionLog, AutomatizacionLogRegistro, CompanyMember
from app.db.repositories.base import UnitOfWork
from app.services.results import EstadoAutomatizacionOut, LogAutomatizacionOut, LogsPage

LIMITE_POR_DEFECTO = 50
LIMITE_MAXIMO = 200
TAREA_PORTAL = "portal"
MAX_CONTEXTO = 12

logger = get_logger(__name__)


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
    origen: str | None = None,
    limit: int = LIMITE_POR_DEFECTO,
    offset: int = 0,
) -> LogsPage:
    """Corridas y fallos del portal de la empresa (cualquier miembro, incluido `lector`).

    `estado` describe a la automatización, así que sale de su última corrida: un fallo del portal
    (que no trae `estado.json` ni pendientes) no debe taparla.
    """
    limite = max(1, min(limit, LIMITE_MAXIMO))
    items, total = await uow.automatizacion_logs.listar(
        access.company_id, resultado=resultado, origen=origen, limit=limite, offset=max(0, offset)
    )
    recientes, _ = await uow.automatizacion_logs.listar(
        access.company_id, resultado=None, origen="automatizacion", limit=1, offset=0
    )
    ultima: AutomatizacionLog | None = recientes[0] if recientes else None
    return LogsPage(items=[_a_salida(x) for x in items], total=total, estado=estado_de(ultima))


def _limpio(texto: str, maximo: int) -> str:
    return redact(" ".join(texto.split()))[:maximo]


async def registrar_fallo_del_portal(
    *,
    uow: UnitOfWork,
    access: CompanyMember,
    user_id: Any,
    accion: str,
    mensaje: str,
    codigo: str | None = None,
    contexto: dict[str, str] | None = None,
) -> bool:
    """Deja en «Logs» un fallo que el usuario vio en el portal (abrir, importar, exportar…).

    Lo avisa el navegador, que es quien sabe qué se le mostró. Se guarda en la misma tabla que las
    corridas de la automatización, con la tarea `portal`. Un `lector` no puede escribir ahí (RLS
    solo deja a admin y emisor): en ese caso devuelve `False` en vez de fallar, porque el aviso
    visible ya se le mostró y un error al registrarlo no debe sumarse al original.
    """
    if access.rol not in (RolEmpresa.admin, RolEmpresa.emisor):
        return False
    accion_limpia = _limpio(accion, 60)
    datos = {
        _limpio(k, 40): _limpio(v, 200) for k, v in list((contexto or {}).items())[:MAX_CONTEXTO]
    }
    await uow.automatizacion_logs.registrar(
        AutomatizacionLogRegistro(
            company_id=access.company_id,
            tareas=[TAREA_PORTAL],
            resultado="con_errores",
            errores=[f"{accion_limpia}: {_limpio(mensaje, 500)}"],
            detalle={
                TAREA_PORTAL: {"accion": accion_limpia, "codigo": _limpio(codigo or "", 60) or None}
                | ({"contexto": datos} if datos else {})
            },
            created_by=user_id,
        )
    )
    logger.warning("Fallo del portal (%s) en %s: %s", accion_limpia, access.ruc, mensaje[:200])
    return True
