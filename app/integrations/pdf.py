"""Generación de la representación impresa (PDF) del comprobante (fase posterior)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.db.models import Comprobante


@runtime_checkable
class PdfRenderer(Protocol):
    """Contrato de generación de PDF."""

    async def render(self, comprobante: Comprobante) -> bytes: ...


class SunatPdfRenderer:
    """PDF con la estructura de la representación impresa de SUNAT. No implementado."""

    def __init__(self, *, logo_url: str | None = None, plantilla: str | None = None) -> None:
        raise NotImplementedError(
            "La generación de PDF es de una fase posterior; la vista previa del "
            "frontend seguirá usando el prototipo hasta entonces"
        )
