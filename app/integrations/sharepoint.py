"""Almacenamiento de PDF/XML/CDR en SharePoint vía Microsoft Graph (fase posterior).

La tabla `archivos` ya guarda la referencia (`drive_id`, `item_id`, `web_url`),
así que el servicio de archivos solo necesitará esta interfaz.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol, runtime_checkable


@dataclass(slots=True)
class ArchivoSubido:
    drive_id: str
    item_id: str
    web_url: str
    path: str
    size_bytes: int


@runtime_checkable
class SharePointGateway(Protocol):
    """Contrato de carga/descarga de archivos en SharePoint."""

    async def upload(
        self, *, ruta: PurePosixPath, contenido: bytes, mime_type: str
    ) -> ArchivoSubido: ...

    async def download(self, *, drive_id: str, item_id: str) -> bytes: ...

    async def create_folder(self, *, ruta: PurePosixPath) -> None: ...


class GraphSharePointGateway:
    """Implementación con Microsoft Graph. Pendiente para la fase de archivos."""

    def __init__(self, *, tenant_id: str, client_id: str, client_secret: str) -> None:
        # Los parámetros llegan desde el SecretsProvider, nunca desde el código.
        raise NotImplementedError(
            "La integración con SharePoint (Microsoft Graph) es de una fase posterior"
        )
