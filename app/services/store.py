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


async def guardar_archivo_de_empresa_sin_fallar(
    *,
    uow: UnitOfWork,
    settings: Settings,
    company_id: UUID,
    tipo: TipoArchivo,
    file_bytes: bytes,
    filename: str,
    folder_path: str,
    storage_provider: StorageProvider | None = None,
) -> Archivo | None:
    """Archiva en R2 un archivo de la empresa que no pertenece a un comprobante.

    Es el caso de la evidencia de SIRE: el ZIP original de un periodo se guarda tal
    como lo entregó SUNAT (con su hash SHA-256) para poder demostrar en una auditoría
    qué tenía registrado SUNAT. Igual que con los demás archivos, un fallo de R2 no
    rompe la operación principal: queda en el log.
    """
    try:
        proveedor = storage_provider or get_storage_provider(settings)
        subido = await proveedor.upload_file(
            file_bytes=file_bytes,
            filename=filename,
            content_type=_tipo_mime(tipo),
            folder_path=folder_path,
        )
        return await uow.archivos.registrar(
            ArchivoRegistro(
                company_id=company_id,
                comprobante_id=None,
                tipo=tipo,
                nombre=filename,
                mime_type=subido.content_type,
                size_bytes=subido.size_bytes,
                hash_sha256=subido.hash_sha256,
                storage_provider=STORAGE_PROVIDER_NOMBRE,
                storage_bucket=settings.r2_bucket_name,
                storage_key=subido.file_key,
                estado=EstadoArchivo.subido,
            )
        )
    except StorageNotConfiguredError:
        logger.warning("No se archivó %s: R2 no está configurado", filename)
    except StorageError:
        logger.warning("No se pudo archivar %s en el almacenamiento", filename)
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
    """Carpeta del comprobante: `comprobantes/{ruc}/{año}/{mes}/{serie}-{número}`.

    Cada documento tiene su propia carpeta para que el XML y el CDR viajen
    juntos al revisar el bucket. El RUC emisor se conserva delante porque el
    bucket es compartido entre empresas: sin él, dos empresas con el mismo
    `FF01-00000001` se pisarían el archivo.
    """
    fecha = comprobante.fecha_emision or datetime.now(tz=UTC).date()
    return (
        f"comprobantes/{comprobante.ruc_emisor}/{fecha.year}/{fecha.month:02d}"
        f"/{documento_del_comprobante(comprobante)}"
    )


def documento_del_comprobante(comprobante: Comprobante) -> str:
    """Número del documento para la ruta: `{serie}-{número}` (ej. `FF01-00016077`).

    Si el registro no trae serie o número (comprobantes incompletos), se usa el
    nombre SUNAT del archivo y, en último caso, un valor fijo: la ruta nunca se
    queda vacía. Los caracteres raros los limpia `construir_clave`.
    """
    serie = comprobante.serie.strip()
    numero = comprobante.numero.strip()
    if serie and numero:
        return f"{serie}-{numero}"
    return comprobante.nombre_archivo.strip() or numero or "archivo"


def _nombre_por_defecto(comprobante: Comprobante, tipo: TipoArchivo) -> str:
    extension = _EXTENSIONES.get(tipo, ".bin")
    base = (comprobante.nombre_archivo or comprobante.numero or "archivo").strip()
    return f"{base}{extension}"


def _tipo_mime(tipo: TipoArchivo) -> str:
    return _MIME_POR_TIPO[tipo]
