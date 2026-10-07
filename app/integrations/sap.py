"""Integración con SAP (fase posterior).

Los campos `n_sap`, `orden_compra` y `check_sap` de `comprobantes` son los que
consumirá esta integración.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.db.models import Comprobante


@runtime_checkable
class SapClient(Protocol):
    """Contrato de intercambio con SAP."""

    async def consultar_orden_compra(self, *, orden_compra: str) -> dict: ...

    async def publicar_comprobante(self, *, comprobante: Comprobante) -> str: ...


class SapSoapClient:
    """Implementación SOAP/RFC contra SAP. No implementada."""

    def __init__(self, *, url: str, usuario: str, password: str) -> None:
        raise NotImplementedError("La integración con SAP es de una fase posterior")
