"""Fábrica del proveedor de almacenamiento.

Cloudflare R2 es el único proveedor. Si en el futuro se añade otro, basta con
registrar su clase aquí.
"""

from __future__ import annotations

from app.core.config import Settings
from app.core.errors import StorageNotConfiguredError
from app.storage.base import StorageProvider
from app.storage.providers.r2 import CloudflareR2StorageProvider


def get_storage_provider(settings: Settings) -> StorageProvider:
    """Devuelve el proveedor configurado o falla con 503 si falta configuración."""
    if not settings.r2_configured:
        raise StorageNotConfiguredError()
    return CloudflareR2StorageProvider(settings=settings)
