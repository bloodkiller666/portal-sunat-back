"""Almacenamiento de archivos del portal de facturación electrónica.

Módulo público:
* `StorageProvider` / `FileUploadResult`: interfaz y resultado de subida.
* `get_storage_provider`: fábrica que construye el proveedor de R2.
* `CloudflareR2StorageProvider`: implementación real (bucket privado, URLs firmadas).
"""

from app.storage.base import (
    FileUploadResult,
    StorageProvider,
    construir_clave,
)
from app.storage.factory import get_storage_provider

__all__ = [
    "CloudflareR2StorageProvider",
    "FileUploadResult",
    "StorageProvider",
    "construir_clave",
    "get_storage_provider",
]


def __getattr__(name: str):
    # Evita importar aioboto3 en el import del paquete.
    if name == "CloudflareR2StorageProvider":
        from app.storage.providers.r2 import CloudflareR2StorageProvider

        return CloudflareR2StorageProvider
    raise AttributeError(name)
