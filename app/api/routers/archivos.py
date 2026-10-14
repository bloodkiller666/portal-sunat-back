"""Endpoints de archivos del comprobante (XML, PDF, CDR) en el bucket privado.

El portal nunca descarga de una URL pública: pide una URL firmada temporal, que
caduca según `R2_SIGNED_URL_EXPIRES_SECONDS`. La subida archiva el PDF que entrega
el sistema de facturación; la descarga es la misma para todos los tipos.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, Path, UploadFile, status

from app.api.deps import CurrentUserDep, SettingsDep, UowDep
from app.api.uploads import leer_con_limite
from app.core.enums import TipoArchivo
from app.services.archivos import (
    TAMANO_MAXIMO_ARCHIVO,
    firmar_descarga,
    list_archivos,
    subir_archivo,
)
from app.services.results import ArchivoOut, ArchivosPage, DescargaOut

router = APIRouter(tags=["archivos"])


@router.get(
    "/comprobantes/{comprobante_id}/archivos",
    response_model=ArchivosPage,
    summary="Lista los archivos almacenados del comprobante",
)
async def get_archivos(
    user: CurrentUserDep,
    uow: UowDep,
    comprobante_id: UUID = Path(description="Identificador del comprobante"),
) -> ArchivosPage:
    """Devuelve XML, PDF y CDR del comprobante; cualquier miembro de la empresa."""
    return await list_archivos(uow=uow, user=user, comprobante_id=comprobante_id)


@router.post(
    "/comprobantes/{comprobante_id}/archivos",
    response_model=ArchivoOut,
    status_code=status.HTTP_201_CREATED,
    summary="Archiva un archivo del comprobante (PDF, XML, CDR o ZIP)",
)
async def post_subir_archivo(
    user: CurrentUserDep,
    uow: UowDep,
    settings: SettingsDep,
    archivo: Annotated[UploadFile, File(description="Archivo a archivar")],
    tipo: Annotated[
        TipoArchivo | None,
        Form(description="Tipo del archivo; si se omite se deduce de la extensión"),
    ] = None,
    comprobante_id: UUID = Path(description="Identificador del comprobante"),
) -> ArchivoOut:
    """Sube el PDF (o XML/CDR) del comprobante al bucket privado (admin/emisor).

    El portal no genera el PDF de la representación impresa: archiva el que
    entrega el sistema de facturación y lo sirve con URL firmada.
    """
    contenido = await leer_con_limite(archivo, TAMANO_MAXIMO_ARCHIVO)
    return await subir_archivo(
        uow=uow,
        settings=settings,
        user=user,
        comprobante_id=comprobante_id,
        file_bytes=contenido,
        filename=archivo.filename,
        tipo=tipo,
    )


@router.post(
    "/archivos/{archivo_id}/download-url",
    response_model=DescargaOut,
    summary="Genera una URL firmada de descarga para el archivo",
)
async def post_download_url(
    user: CurrentUserDep,
    uow: UowDep,
    settings: SettingsDep,
    archivo_id: UUID = Path(description="Identificador del archivo"),
) -> DescargaOut:
    """Firma una URL temporal (bucket privado). Sin credenciales de R2: 503."""
    return await firmar_descarga(
        uow=uow, settings=settings, user=user, archivo_id=archivo_id
    )
