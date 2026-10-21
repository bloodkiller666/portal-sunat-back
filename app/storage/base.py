"""Almacenamiento de archivos (PDF, XML, CDR) del portal multiempresa.

Interfaz única `StorageProvider`. La implementación real es
`CloudflareR2StorageProvider` (API S3 compatible de Cloudflare R2) sobre un
bucket **privado**: los archivos no se sirven con URLs públicas, sino con URLs
firmadas de vida corta (`get_download_url`).

El servicio de archivos (`app/services/store.py`) es quien arma la ruta del
objeto y registra la referencia en la tabla `archivos`; el proveedor solo sube
bytes y firma URLs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

_INVALIDOS_EN_CLAVE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class FileUploadResult:
    """Resultado de `upload_file`: lo que el servicio de archivos necesita."""

    file_key: str
    size_bytes: int
    content_type: str
    hash_sha256: str


@runtime_checkable
class StorageProvider(Protocol):
    """Contrato de almacenamiento de archivos."""

    async def upload_file(
        self,
        file_bytes: bytes,
        filename: str,
        content_type: str,
        folder_path: str,
    ) -> FileUploadResult:
        """Sube `file_bytes` a `folder_path/filename` y devuelve su referencia."""
        ...

    async def get_download_url(self, file_key: str, *, expires_in: int | None = None) -> str:
        """URL firmada y temporal para descargar `file_key` (bucket privado)."""
        ...


def sanear_componente(valor: str) -> str:
    """Normaliza un fragmento de la ruta para que sea seguro como clave S3.

    Quita separadores y cualquier intento de `..` (path traversal), de modo que
    el objeto siempre quede dentro de la carpeta indicada.
    """
    limpio = valor.strip().replace("\\", "_")
    limpio = _INVALIDOS_EN_CLAVE.sub("_", limpio)
    limpio = limpio.replace("..", "_")
    return limpio.strip("./_") or "archivo"


def normalizar_clave(file_key: str) -> str:
    """Valida la clave S3 completa (conserva las barras) y rechaza `..`."""
    clave = file_key.strip().lstrip("/")
    if not clave or any(segmento in ("", "..") for segmento in clave.split("/")):
        raise ValueError("La clave del objeto no es válida")
    return clave


def construir_clave(folder_path: str, filename: str) -> str:
    """Une la carpeta y el nombre en una clave S3 sin barras duplicadas.

    La ruta final es `comprobantes/{ruc}/{año}/{mes}/{serie}-{número}/{filename}`; cada
    componente se sanea por separado para que nada raro llegue al bucket.
    """
    partes = [sanear_componente(parte) for parte in folder_path.strip("/").split("/") if parte]
    partes.append(sanear_componente(filename))
    return "/".join(partes)
