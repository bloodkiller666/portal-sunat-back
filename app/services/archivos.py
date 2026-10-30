"""Servicio de archivos de un comprobante: listado y URL firmada de descarga.

El bucket es privado: la descarga siempre pasa por una URL firmada temporal. Este
servicio solo resuelve el permiso (membresía en la empresa del archivo) y delega
la firma en el `StorageProvider`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.core.config import Settings
from app.core.enums import ROLES_OPERATIVOS, OrigenComprobante, TipoArchivo
from app.core.errors import InvalidDocumentError, NotFoundError
from app.core.security import AuthenticatedUser
from app.db.models import Archivo
from app.db.repositories.base import UnitOfWork
from app.services.carpeta_local import leer_correos_txt_locales
from app.services.companies import require_membership
from app.services.correo_compras import texto_correo_de_compra
from app.services.results import (
    ArchivoOut,
    ArchivosPage,
    CorreoTxtOut,
    DescargaOut,
    PrevisualizacionItemOut,
    PrevisualizacionOut,
)
from app.services.store import sube_archivo, url_descarga
from app.storage.base import StorageProvider

_MAX_DIAS_FIRMA = 7

# Tope de un archivo suelto (PDF, XML o ZIP): el XML del comprobante usa 5 MB.
TAMANO_MAXIMO_ARCHIVO = 20 * 1024 * 1024

# Extensión -> tipo de archivo (para no obligar al frontend a enviarlo).
_TIPO_POR_EXTENSION = {
    ".pdf": TipoArchivo.pdf,
    ".xml": TipoArchivo.xml,
    ".zip": TipoArchivo.zip,
    ".cdr": TipoArchivo.cdr,
}

_TIPO_POR_MIME = {
    "application/pdf": TipoArchivo.pdf,
    "application/xml": TipoArchivo.xml,
    "text/xml": TipoArchivo.xml,
    "application/zip": TipoArchivo.zip,
}


def inferir_tipo(filename: str | None, content_type: str | None) -> TipoArchivo | None:
    """Deduce el tipo de archivo por extensión y, si no, por MIME."""
    nombre = (filename or "").lower()
    for extension, tipo in _TIPO_POR_EXTENSION.items():
        if nombre.endswith(extension):
            return tipo
    mime = (content_type or "").split(";")[0].strip().lower()
    return _TIPO_POR_MIME.get(mime)


def to_archivo_out(archivo: Archivo) -> ArchivoOut:
    return ArchivoOut(
        id=archivo.id,
        comprobante_id=archivo.comprobante_id,
        tipo=archivo.tipo,
        nombre=archivo.nombre,
        mime_type=archivo.mime_type,
        size_bytes=archivo.size_bytes,
        hash_sha256=archivo.hash_sha256,
        estado=archivo.estado,
        subido_at=archivo.subido_at,
        created_at=archivo.created_at,
    )


async def list_archivos(
    *, uow: UnitOfWork, user: AuthenticatedUser, comprobante_id: UUID
) -> ArchivosPage:
    """Lista los archivos del comprobante (cualquier miembro, incluido `lector`)."""
    comprobante = await uow.comprobantes.get(comprobante_id)
    if comprobante is None:
        # RLS oculta los comprobantes de otras empresas: 404 sin filtrar existencia.
        raise NotFoundError("El comprobante no existe o no pertenece a sus empresas")

    await require_membership(uow, user, comprobante.company_id)

    archivos = await uow.archivos.list_for_comprobante(comprobante_id)
    return ArchivosPage(
        comprobante_id=comprobante_id,
        items=[to_archivo_out(archivo) for archivo in archivos],
        total=len(archivos),
    )


_ORDEN_PREVISUALIZACION = {TipoArchivo.pdf: 0, TipoArchivo.xml: 1, TipoArchivo.cdr: 2}


def _mime_previsualizable(archivo: Archivo) -> str | None:
    """El MIME con que se muestra en línea, o `None` si no se puede mostrar.

    Se ven los PDF, los XML y los CDR que llegaron como XML (`CDR-….xml`, el de los proveedores);
    el `R-….zip` de SUNAT es un ZIP y solo se descarga.
    """
    if archivo.tipo is TipoArchivo.pdf:
        return "application/pdf"
    if archivo.tipo is TipoArchivo.xml:
        return "application/xml"
    if archivo.tipo is TipoArchivo.cdr and archivo.nombre.lower().endswith(".xml"):
        return "application/xml"
    return None


async def previsualizar_comprobante(
    *,
    uow: UnitOfWork,
    settings: Settings,
    user: AuthenticatedUser,
    comprobante_id: UUID,
    storage_provider: StorageProvider | None = None,
) -> PrevisualizacionOut:
    """Los PDF y XML del comprobante, cada uno con su URL firmada para mostrarlo en línea.

    Una sola petición en vez de «lista» + «una URL por archivo»: cada viaje a la base cuesta
    ~300 ms, y firmar una URL no toca la red (es una cuenta local). Así abrir la previsualización
    y pasar al siguiente archivo no vuelve a pedir nada al servidor.
    """
    comprobante = await uow.comprobantes.get(comprobante_id)
    if comprobante is None:
        raise NotFoundError("El comprobante no existe o no pertenece a sus empresas")
    await require_membership(uow, user, comprobante.company_id)

    archivos = [
        a
        for a in await uow.archivos.list_for_comprobante(comprobante_id)
        if a.storage_key and _mime_previsualizable(a)
    ]
    # Primero los PDF (lo que se lee), luego los XML y al final los CDR, cada uno por nombre.
    archivos.sort(key=lambda a: (_ORDEN_PREVISUALIZACION[a.tipo], a.nombre.lower()))
    vigencia = min(settings.r2_signed_url_expires_seconds, _MAX_DIAS_FIRMA * 86400)
    items = [
        PrevisualizacionItemOut(
            id=a.id,
            tipo=a.tipo.value,
            nombre=a.nombre,
            size_bytes=a.size_bytes,
            url=await url_descarga(
                settings=settings,
                archivo=a,
                storage_provider=storage_provider,
                inline_mime=_mime_previsualizable(a),
            ),
        )
        for a in archivos
    ]
    correos: list[CorreoTxtOut] = []
    if comprobante.origen is OrigenComprobante.recibido:
        # Los de la PC traen también el cuerpo; sin ellos (servidor) se arma uno con lo que guarda
        # la base.
        locales = await asyncio.to_thread(leer_correos_txt_locales, settings, comprobante)
        correos = [CorreoTxtOut(nombre=n, texto=t) for n, t in locales]
        if not correos:
            texto = texto_correo_de_compra(comprobante, archivos)
            if texto is not None:
                correos = [CorreoTxtOut(nombre="correo.txt", texto=texto)]
    return PrevisualizacionOut(
        comprobante_id=comprobante_id,
        items=items,
        expires_in=vigencia,
        correos_txt=correos,
        correo_txt=correos[0].texto if correos else None,
    )


async def subir_archivo(
    *,
    uow: UnitOfWork,
    settings: Settings,
    user: AuthenticatedUser,
    comprobante_id: UUID,
    file_bytes: bytes,
    filename: str | None = None,
    tipo: TipoArchivo | None = None,
    storage_provider: StorageProvider | None = None,
) -> ArchivoOut:
    """Archiva un archivo del comprobante (el PDF de la representación impresa,
    el XML del proveedor, el ZIP del CDR...) en el bucket privado.

    El portal no genera el PDF: lo archiva tal como lo entrega el sistema de
    facturación. Requiere rol admin o emisor.
    """
    if not file_bytes:
        raise InvalidDocumentError("El archivo está vacío")

    comprobante = await uow.comprobantes.get(comprobante_id)
    if comprobante is None:
        raise NotFoundError("El comprobante no existe o no pertenece a sus empresas")

    await require_membership(uow, user, comprobante.company_id, roles=ROLES_OPERATIVOS)

    tipo_efectivo = tipo or inferir_tipo(filename, None)
    if tipo_efectivo is None:
        raise InvalidDocumentError(
            "No se pudo determinar el tipo de archivo: envíe el parámetro `tipo` "
            "o use una extensión .pdf, .xml, .zip o .cdr"
        )

    archivo = await sube_archivo(
        uow=uow,
        settings=settings,
        comprobante_id=comprobante_id,
        tipo=tipo_efectivo,
        file_bytes=file_bytes,
        filename=filename,
        storage_provider=storage_provider,
    )
    return to_archivo_out(archivo)


async def firmar_descarga(
    *,
    uow: UnitOfWork,
    settings: Settings,
    user: AuthenticatedUser,
    archivo_id: UUID,
    storage_provider: StorageProvider | None = None,
) -> DescargaOut:
    """Devuelve una URL firmada temporal para descargar el archivo."""
    archivo = await uow.archivos.get(archivo_id)
    if archivo is None:
        raise NotFoundError("El archivo no existe o no pertenece a sus empresas")

    await require_membership(uow, user, archivo.company_id)

    url = await url_descarga(settings=settings, archivo=archivo, storage_provider=storage_provider)
    vigencia = min(settings.r2_signed_url_expires_seconds, _MAX_DIAS_FIRMA * 86400)
    return DescargaOut(
        archivo_id=archivo.id,
        nombre=archivo.nombre,
        url=url,
        expires_in=vigencia,
        expira_at=datetime.now(tz=UTC) + timedelta(seconds=vigencia),
    )
