"""Importación masiva de N° SAP desde el Excel que arma Contabilidad.

El portal no consulta SAP: el número llega en un Excel con el documento (serie-número) y,
si se quiere afinar, el RUC. Cada fila se empareja con un comprobante ya registrado; lo que
no esté en el portal (o venga mal escrito) se omite y se cuenta, sin detener la carga ni
dar error: el Excel casi nunca coincide al 100 % con lo sincronizado.
"""

from __future__ import annotations

import re

from app.db.models import CompanyMember
from app.db.repositories.base import FilaSap, UnitOfWork
from app.services.results import SapImportOut

MAX_FILAS_SAP = 20_000
MAX_LONGITUD_SAP = 50

# `F001-123`, `F001-00000123`, `f001 123`, `F001/123`: la serie son 4 caracteres.
_DOCUMENTO_RE = re.compile(r"^([A-Z0-9]{4})\s*[-/ ]\s*0*([0-9]{1,8})$")
_RUC_RE = re.compile(r"^[0-9]{11}$")


def normalizar_fila(documento: str | None, ruc: str | None, n_sap: str | None) -> FilaSap | None:
    """Convierte una fila cruda en `FilaSap`; `None` si no se puede identificar el documento."""
    coincidencia = _DOCUMENTO_RE.match((documento or "").strip().upper())
    sap = (n_sap or "").strip()
    if coincidencia is None or not sap or len(sap) > MAX_LONGITUD_SAP:
        return None
    correlativo = int(coincidencia.group(2))
    if correlativo <= 0:
        return None
    ruc_limpio = (ruc or "").strip()
    return FilaSap(
        serie=coincidencia.group(1),
        correlativo=correlativo,
        n_sap=sap,
        ruc=ruc_limpio if _RUC_RE.match(ruc_limpio) else None,
    )


async def importar_sap(
    *,
    uow: UnitOfWork,
    access: CompanyMember,
    filas: list[tuple[str | None, str | None, str | None]],
) -> SapImportOut:
    """Registra los N° SAP de las filas que existan en la empresa.

    El permiso (admin/emisor) lo exige la dependencia del endpoint.
    """
    validas: list[FilaSap] = []
    for documento, ruc, n_sap in filas:
        fila = normalizar_fila(documento, ruc, n_sap)
        if fila is not None:
            validas.append(fila)

    encontradas = await uow.comprobantes.asignar_n_sap_lote(access.company_id, validas)
    return SapImportOut(
        ruc=access.ruc,
        recibidas=len(filas),
        actualizadas=len(encontradas),
        no_encontradas=len(validas) - len(encontradas),
        invalidas=len(filas) - len(validas),
    )


__all__ = ["MAX_FILAS_SAP", "importar_sap", "normalizar_fila"]
