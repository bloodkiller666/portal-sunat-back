"""Servicio de almacenamiento de archivos (PDF, XML, CDR) en Cloudflare R2.

Sube el archivo al bucket privado y registra su referencia en la tabla
`archivos` (clave S3, bucket, MIME, tamaño y hash SHA-256 reales). La descarga
se hace después con una URL firmada temporal (`url_descarga`), nunca con una URL
pública. Este servicio no toca SUNAT.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from app.core.config import Settings
from app.core.enums import EstadoArchivo, TipoArchivo
from app.core.errors import (
    NotFoundError,
    StorageError,
    StorageNotConfiguredError,
)
from app.core.logging import get_logger
from app.db.models import Archivo, ArchivoRegistro, Comprobante
from app.db.repositories.base import UnitOfWork
from app.storage.base import StorageProvider
from app.storage.factory import get_storage_provider

logger = get_logger(__name__)

STORAGE_PROVIDER_NOMBRE = "r2"

# Extensión por defecto para cada tipo de archivo.
_EXTENSIONES = {
    TipoArchivo.pdf: ".pdf",
    TipoArchivo.xml: ".xml",
    TipoArchivo.cdr: ".zip",
    TipoArchivo.zip: ".zip",
}

_MIME_POR_TIPO = {
    TipoArchivo.pdf: "application/pdf",
    TipoArchivo.xml: "application/xml",
    TipoArchivo.cdr: "application/zip",
    TipoArchivo.zip: "application/zip",
}


async def sube_archivo(
    *,
    uow: UnitOfWork,
    settings: Settings,
    comprobante_id: UUID,
    tipo: TipoArchivo,
    file_bytes: bytes,
    filename: str | None = None,
    folder_path: str | None = None,
    storage_provider: StorageProvider | None = None,
) -> Archivo:
    """Sube el archivo a R2 y guarda su referencia en `archivos`.

    Regresa el objeto `Archivo` recién insertado con la clave S3 y el hash real.
    """
    comprobante = await uow.comprobantes.get(comprobante_id)
    if comprobante is None:
        raise NotFoundError("El comprobante no existe o no pertenece a sus empresas")

    if not file_bytes:
        raise StorageError("El archivo está vacío")

    nombre = filename or _nombre_por_defecto(comprobante, tipo)
    carpeta = folder_path or carpeta_del_comprobante(comprobante)
    proveedor = storage_provider or get_storage_provider(settings)

    subido = await proveedor.upload_file(
        file_bytes=file_bytes,
        filename=nombre,
        content_type=_tipo_mime(tipo),
        folder_path=carpeta,
    )

    registro = ArchivoRegistro(
        company_id=comprobante.company_id,
        comprobante_id=comprobante.id,
        tipo=tipo,
        nombre=nombre,
        mime_type=subido.content_type,
        size_bytes=subido.size_bytes,
        hash_sha256=subido.hash_sha256,
        storage_provider=STORAGE_PROVIDER_NOMBRE,
        storage_bucket=settings.r2_bucket_name,
        storage_key=subido.file_key,
        estado=EstadoArchivo.subido,
    )
    return await uow.archivos.registrar(registro)


async def guardar_archivo_sin_fallar(
    *,
    uow: UnitOfWork,
    settings: Settings,
    comprobante_id: UUID,
    tipo: TipoArchivo,
    file_bytes: bytes,
    filename: str | None = None,
    folder_path: str | None = None,
    storage_provider: StorageProvider | None = None,
) -> Archivo | None:
    """Sube el archivo y registra su referencia, sin romper la operación principal.

    Guardar los archivos es un extra: si R2 no está configurado en este entorno o
    la subida falla, la importación o el refresco del CDR deben completarse igual.
    El problema queda en el log (nunca con credenciales).
    """
    try:
        return await sube_archivo(
            uow=uow,
            settings=settings,
            comprobante_id=comprobante_id,
            tipo=tipo,
            file_bytes=file_bytes,
            filename=filename,
            folder_path=folder_path,
            storage_provider=storage_provider,
        )
    except StorageNotConfiguredError:
        logger.warning(
            "No se guardó el archivo %s del comprobante %s: R2 no está configurado",
            tipo.value,
            comprobante_id,
        )
    except StorageError:
        logger.warning(
            "No se pudo guardar el archivo %s del comprobante %s en el almacenamiento",
            tipo.value,
            comprobante_id,
        )
    return None


async def url_descarga(
    *,
    settings: Settings,
    archivo: Archivo,
    storage_provider: StorageProvider | None = None,
    expires_in: int | None = None,
) -> str:
    """URL firmada temporal (bucket privado) para descargar un archivo."""
    if not archivo.storage_key:
        raise StorageError("El archivo no tiene una clave de almacenamiento registrada")
    proveedor = storage_provider or get_storage_provider(settings)
    return await proveedor.get_download_url(archivo.storage_key, expires_in=expires_in)


def carpeta_del_comprobante(comprobante: Comprobante) -> str:
    """Carpeta base del comprobante: `comprobantes/{ruc}/{año}/{mes}`."""
    fecha = comprobante.fecha_emision or datetime.now(tz=UTC).date()
    return f"comprobantes/{comprobante.ruc_emisor}/{fecha.year}/{fecha.month:02d}"


def _nombre_por_defecto(comprobante: Comprobante, tipo: TipoArchivo) -> str:
    extension = _EXTENSIONES.get(tipo, ".bin")
    base = (comprobante.nombre_archivo or comprobante.numero or "archivo").strip()
    return f"{base}{extension}"


def _tipo_mime(tipo: TipoArchivo) -> str:
    return _MIME_POR_TIPO[tipo]
