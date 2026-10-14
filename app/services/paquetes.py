"""Importación masiva de un paquete ZIP con los XML (y sus PDF) del sistema emisor.

Es la vía práctica para cargar facturas reales en el portal: el sistema que emite
(o la descarga del portal de SUNAT) entrega un ZIP con los XML y, normalmente,
también los PDF de la representación impresa. El paquete se procesa comprobante
por comprobante: un XML con error no detiene el resto.

El PDF que acompaña a un XML se archiva asociado a ese comprobante (el portal no
genera el PDF: guarda el que ya existe).
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from pathlib import PurePosixPath

from app.core.config import Settings
from app.core.enums import OrigenComprobante, TipoArchivo
from app.core.errors import AppError, FileTooLargeError, InvalidDocumentError
from app.core.logging import get_logger
from app.core.redaction import redact
from app.core.security import AuthenticatedUser
from app.db.models import CompanyMember
from app.db.repositories.base import UnitOfWork
from app.services.comprobantes import import_comprobante_xml
from app.services.results import PaqueteItemOut, PaqueteOut
from app.services.store import guardar_archivo_sin_fallar

logger = get_logger(__name__)

# Códigos que no cuentan como error del comprobante: se informan aparte.
_CODIGOS_INFORMATIVOS = frozenset({"conflict", "sin_xml"})

# Topes del paquete: un ZIP de comprobantes nunca se acerca a esto.
TAMANO_MAXIMO_PAQUETE = 50 * 1024 * 1024  # ZIP tal como llega
MAX_ARCHIVOS_PAQUETE = 200
MAX_BYTES_DESCOMPRIMIDOS = 150 * 1024 * 1024
MAX_BYTES_POR_ENTRADA = 25 * 1024 * 1024


def _nombre_base(nombre: str) -> str:
    """Nombre del archivo sin carpetas (el ZIP puede traer subdirectorios)."""
    return PurePosixPath(nombre.replace("\\", "/")).name


async def importar_paquete(
    *,
    uow: UnitOfWork,
    settings: Settings,
    user: AuthenticatedUser,
    access: CompanyMember,
    paquete: bytes,
    origen: OrigenComprobante = OrigenComprobante.emitido,
) -> PaqueteOut:
    """Importa todos los XML del ZIP y archiva los PDF que los acompañen."""
    if not paquete:
        raise InvalidDocumentError("El paquete está vacío")
    if not paquete.startswith(b"PK"):
        raise InvalidDocumentError("El archivo no es un paquete ZIP")

    try:
        with zipfile.ZipFile(io.BytesIO(paquete)) as archivo_zip:
            entradas = _entradas_validas(archivo_zip)
            xmls = {clave: info for clave, info in entradas.items() if clave.endswith(".xml")}
            pdfs = {clave: info for clave, info in entradas.items() if clave.endswith(".pdf")}
            items = [
                await _importar_xml(
                    archivo_zip=archivo_zip,
                    info=info,
                    nombre=clave,
                    pdf=pdfs.get(f"{clave[:-4]}.pdf"),
                    uow=uow,
                    settings=settings,
                    user=user,
                    access=access,
                    origen=origen,
                )
                for clave, info in xmls.items()
            ]
    except zipfile.BadZipFile as exc:
        raise InvalidDocumentError("El paquete ZIP está dañado o incompleto") from exc

    if not xmls:
        raise InvalidDocumentError("El paquete no contiene ningún archivo XML")

    # PDF sin XML: se informa para que el usuario revise el paquete.
    sueltos = sorted(set(pdfs) - {f"{clave[:-4]}.pdf" for clave in xmls})
    items.extend(
        PaqueteItemOut(
            archivo=_nombre_base(pdfs[clave].filename),
            ok=False,
            code="sin_xml",
            mensaje="El PDF no tiene un XML con el mismo nombre en el paquete",
        )
        for clave in sueltos
    )

    return PaqueteOut(
        ruc=access.company.ruc,
        origen=origen,
        total=len(items),
        importados=sum(1 for item in items if item.ok),
        con_pdf=sum(1 for item in items if item.pdf),
        duplicados=sum(1 for item in items if item.code == "conflict"),
        con_error=sum(
            1 for item in items if not item.ok and item.code not in _CODIGOS_INFORMATIVOS
        ),
        sin_xml=sum(1 for item in items if item.code == "sin_xml"),
        items=items,
        procesado_at=datetime.now(tz=UTC),
    )


def _entradas_validas(archivo_zip: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    """Filtra el contenido del ZIP y aplica los topes (nada de bombas zip)."""
    entradas: dict[str, zipfile.ZipInfo] = {}
    total_descomprimido = 0

    for info in archivo_zip.infolist():
        if info.is_dir():
            continue
        nombre = _nombre_base(info.filename)
        if not nombre or nombre.startswith("."):
            continue
        if info.file_size > MAX_BYTES_POR_ENTRADA:
            raise FileTooLargeError(
                details={"archivo": nombre, "maximo_bytes": MAX_BYTES_POR_ENTRADA}
            )
        total_descomprimido += info.file_size
        if total_descomprimido > MAX_BYTES_DESCOMPRIMIDOS:
            raise FileTooLargeError(
                details={
                    "maximo_bytes_descomprimidos": MAX_BYTES_DESCOMPRIMIDOS,
                    "recibido_bytes": total_descomprimido,
                }
            )
        # Dos entradas con el mismo nombre: manda la última.
        entradas[nombre.lower()] = info

    if len(entradas) > MAX_ARCHIVOS_PAQUETE:
        raise FileTooLargeError(
            details={"maximo_archivos": MAX_ARCHIVOS_PAQUETE, "recibidos": len(entradas)}
        )
    return entradas


async def _importar_xml(
    *,
    archivo_zip: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    nombre: str,
    pdf: zipfile.ZipInfo | None,
    uow: UnitOfWork,
    settings: Settings,
    user: AuthenticatedUser,
    access: CompanyMember,
    origen: OrigenComprobante,
) -> PaqueteItemOut:
    """Importa un XML del paquete y, si hay, archiva el PDF que lo acompaña."""
    item = PaqueteItemOut(archivo=_nombre_base(info.filename))
    try:
        comprobante = await import_comprobante_xml(
            uow=uow,
            settings=settings,
            user=user,
            access=access,
            xml=archivo_zip.read(info.filename),
            origen=origen,
        )
    except AppError as exc:
        item.ok = False
        item.code = exc.code
        item.mensaje = redact(exc.message)
        return item

    item.comprobante_id = comprobante.id
    item.numero = comprobante.nombre_archivo or comprobante.numero

    if pdf is not None:
        guardado = await guardar_archivo_sin_fallar(
            uow=uow,
            settings=settings,
            comprobante_id=comprobante.id,
            tipo=TipoArchivo.pdf,
            file_bytes=archivo_zip.read(pdf.filename),
            filename=_nombre_base(pdf.filename),
        )
        item.pdf = guardado is not None
    return item
