"""Carga masiva de CDR (constancias de recepción) desde un ZIP.

Para los comprobantes que SUNAT lista en SIRE pero que emite otro sistema (un OSE,
un PSE, SAP...), la consulta `getStatusCdr` no devuelve CDR (código `0013`). El CDR
lo tiene quien envió el comprobante a SUNAT, y esta es la vía para traerlo al portal:
un ZIP con los archivos `R-{RUC}-{tipo}-{serie}-{numero}.zip` (o `.xml`).

Cada CDR se asocia por su nombre al comprobante emitido de la empresa, se archiva en
R2 con su hash y actualiza el estado según lo que dice la constancia. Un archivo con
problemas no detiene el resto, y un comprobante que ya tiene CDR no se pisa.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import UTC, datetime
from pathlib import PurePosixPath

from app.core.config import Settings
from app.core.enums import TipoArchivo
from app.core.errors import AppError, FileTooLargeError, InvalidDocumentError
from app.core.logging import get_logger
from app.core.redaction import redact
from app.core.security import AuthenticatedUser
from app.db.models import CompanyMember
from app.db.repositories.base import UnitOfWork
from app.services.comprobantes import siguiente_estado
from app.services.results import CdrItemOut, CdrPaqueteOut
from app.services.store import guardar_archivo_sin_fallar
from app.sunat.cdr import parse_cdr_xml, parse_cdr_zip
from app.sunat.models import CdrInfo
from app.sunat.states import map_cdr_a_estado

logger = get_logger(__name__)

TAMANO_MAXIMO_PAQUETE = 50 * 1024 * 1024
MAX_ARCHIVOS_PAQUETE = 500
MAX_BYTES_POR_ENTRADA = 5 * 1024 * 1024
MAX_BYTES_DESCOMPRIMIDOS = 150 * 1024 * 1024

# R-{RUC}-{tipo}-{serie}-{numero}.zip|xml (nomenclatura de SUNAT para el CDR).
_NOMBRE_CDR = re.compile(
    r"^R-(?P<ruc>\d{11})-(?P<tipo>\d{2})-(?P<serie>[A-Z0-9]{1,4})-(?P<numero>\d{1,8})"
    r"\.(?P<ext>zip|xml)$",
    re.IGNORECASE,
)


def _nombre_base(nombre: str) -> str:
    return PurePosixPath(nombre.replace("\\", "/")).name


async def importar_cdrs(
    *,
    uow: UnitOfWork,
    settings: Settings,
    user: AuthenticatedUser,
    access: CompanyMember,
    paquete: bytes,
) -> CdrPaqueteOut:
    """Asocia cada CDR del ZIP a su comprobante emitido y lo archiva."""
    if not paquete:
        raise InvalidDocumentError("El paquete está vacío")
    if not paquete.startswith(b"PK"):
        raise InvalidDocumentError("El archivo no es un paquete ZIP")

    try:
        with zipfile.ZipFile(io.BytesIO(paquete)) as archivo_zip:
            entradas = _entradas_validas(archivo_zip)
            items = [
                await _procesar(
                    archivo_zip=archivo_zip,
                    info=info,
                    uow=uow,
                    settings=settings,
                    access=access,
                )
                for info in entradas
            ]
    except zipfile.BadZipFile as exc:
        raise InvalidDocumentError("El paquete ZIP está dañado o incompleto") from exc

    if not items:
        raise InvalidDocumentError("El paquete no contiene archivos de CDR")

    return CdrPaqueteOut(
        ruc=access.company.ruc,
        total=len(items),
        asociados=sum(1 for item in items if item.ok),
        ya_tenian=sum(1 for item in items if item.code == "ya_tiene_cdr"),
        sin_comprobante=sum(1 for item in items if item.code == "sin_comprobante"),
        con_error=sum(
            1
            for item in items
            if not item.ok and item.code not in ("ya_tiene_cdr", "sin_comprobante")
        ),
        items=items,
        procesado_at=datetime.now(tz=UTC),
    )


def _entradas_validas(archivo_zip: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Filtra el ZIP y aplica los topes (nada de bombas zip)."""
    entradas: dict[str, zipfile.ZipInfo] = {}
    total = 0
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
        total += info.file_size
        if total > MAX_BYTES_DESCOMPRIMIDOS:
            raise FileTooLargeError(
                details={"maximo_bytes_descomprimidos": MAX_BYTES_DESCOMPRIMIDOS}
            )
        entradas[nombre.lower()] = info
    if len(entradas) > MAX_ARCHIVOS_PAQUETE:
        raise FileTooLargeError(
            details={"maximo_archivos": MAX_ARCHIVOS_PAQUETE, "recibidos": len(entradas)}
        )
    return list(entradas.values())


def leer_cdr(contenido: bytes, extension: str) -> tuple[CdrInfo, bytes]:
    """Interpreta el CDR y devuelve también el ZIP que se archivará."""
    if extension == "xml":
        cdr = parse_cdr_xml(contenido)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("cdr.xml", contenido)
        return cdr, buffer.getvalue()
    return parse_cdr_zip(contenido), contenido


async def _procesar(
    *,
    archivo_zip: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    uow: UnitOfWork,
    settings: Settings,
    access: CompanyMember,
) -> CdrItemOut:
    nombre = _nombre_base(info.filename)
    item = CdrItemOut(archivo=nombre)

    coincidencia = _NOMBRE_CDR.match(nombre)
    if coincidencia is None:
        return _falla(item, "nombre_invalido", "El nombre debe ser R-{RUC}-{tipo}-{serie}-{numero}")
    if coincidencia["ruc"] != access.company.ruc:
        return _falla(item, "ruc_distinto", "El CDR pertenece a otro RUC")

    comprobante = await uow.comprobantes.find_emitido(
        access.company_id,
        tipo_documento=coincidencia["tipo"],
        serie=coincidencia["serie"].upper(),
        correlativo=int(coincidencia["numero"]),
    )
    if comprobante is None:
        return _falla(item, "sin_comprobante", "No hay un comprobante emitido con esa numeración")
    item.comprobante_id = comprobante.id
    item.numero = comprobante.nombre_archivo or comprobante.numero

    archivados = await uow.archivos.list_for_comprobante(comprobante.id)
    if any(archivo.tipo is TipoArchivo.cdr for archivo in archivados):
        return _falla(item, "ya_tiene_cdr", "El comprobante ya tiene un CDR archivado")

    try:
        cdr, zip_bytes = leer_cdr(archivo_zip.read(info.filename), coincidencia["ext"].lower())
    except AppError as exc:
        return _falla(item, exc.code, redact(exc.message))

    guardado = await guardar_archivo_sin_fallar(
        uow=uow,
        settings=settings,
        comprobante_id=comprobante.id,
        tipo=TipoArchivo.cdr,
        file_bytes=zip_bytes,
        filename=f"{nombre.rsplit('.', 1)[0]}.zip",
    )
    if guardado is None:
        # Sin archivo en R2 no hay evidencia: no se cambia el estado a ciegas.
        return _falla(item, "no_archivado", "No se pudo guardar el CDR en el almacenamiento")

    estado = siguiente_estado(comprobante.estado, map_cdr_a_estado(cdr))
    actualizado = await uow.comprobantes.update_sunat_state(
        comprobante.id,
        estado=estado,
        cdr_codigo=cdr.codigo,
        cdr_descripcion=cdr.descripcion,
        cdr_notas=cdr.notas,
    )
    item.estado = actualizado.estado
    return item


def _falla(item: CdrItemOut, codigo: str, mensaje: str) -> CdrItemOut:
    item.ok = False
    item.code = codigo
    item.mensaje = mensaje
    return item


__all__ = ["importar_cdrs", "leer_cdr"]
