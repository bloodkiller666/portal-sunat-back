"""Importación de compras desde un ZIP organizado por carpetas (proveedores).

Es la vía para registrar a mano lo que llegó por correo y SIRE no refleja (o refleja sin XML):
cada carpeta del ZIP reúne los archivos de UNA compra (`RUC/SERIE-NÚMERO/…`), pero también se
acepta un ZIP plano. A diferencia de `paquetes.py` (que empareja el PDF por el mismo nombre que
el XML y solo sirve para ventas), aquí los archivos se agrupan por carpeta:

* El XML de la factura/nota se importa con `origen=recibido` y completa la compra de SIRE si ya
  existe. Un XML repetido no es un error: sigue sirviendo para añadir el PDF o el CDR que falte.
* Con UN solo comprobante en la carpeta, todos los PDF y el CDR de esa carpeta son suyos. Con
  varios (ZIP plano), el PDF se une por el nombre o por la serie-número que lleve, y el CDR por
  la referencia que trae dentro.
* Las guías de remisión y los XML no reconocidos se ignoran y se informan.

Las reglas de agrupación son puras (`agrupar`) y se prueban sin base de datos.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath

from lxml import etree

from app.core.config import Settings
from app.core.enums import OrigenComprobante, TipoArchivo
from app.core.errors import AppError, FileTooLargeError, InvalidDocumentError
from app.core.logging import get_logger
from app.core.redaction import redact
from app.core.security import AuthenticatedUser
from app.db.models import CompanyMember
from app.db.repositories.base import UnitOfWork
from app.services.cdr_paquete import leer_cdr
from app.services.comprobantes import import_comprobante_xml, siguiente_estado
from app.services.results import PaqueteItemOut, PaqueteOut
from app.services.store import guardar_archivo_sin_fallar
from app.sunat.states import map_cdr_a_estado
from app.sunat.ubl import parse_comprobante_ubl

logger = get_logger(__name__)

TAMANO_MAXIMO_PAQUETE = 50 * 1024 * 1024
MAX_ARCHIVOS_PAQUETE = 400
MAX_BYTES_DESCOMPRIMIDOS = 200 * 1024 * 1024
MAX_BYTES_POR_ENTRADA = 25 * 1024 * 1024

_RAICES_COMPROBANTE = frozenset({"Invoice", "CreditNote", "DebitNote"})
_SERIE_NUMERO = re.compile(r"(?<![A-Z0-9])([A-Z0-9]{4})-0*(\d{1,8})(?!\d)", re.IGNORECASE)
_PARSER_SEGURO = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)


@dataclass(frozen=True)
class Entrada:
    """Un archivo del paquete, ya leído."""

    ruta: str  # con carpetas, separadas por `/`
    contenido: bytes

    @property
    def carpeta(self) -> str:
        return str(PurePosixPath(self.ruta).parent)

    @property
    def nombre(self) -> str:
        return PurePosixPath(self.ruta).name

    @property
    def extension(self) -> str:
        return self.nombre.rsplit(".", 1)[-1].lower() if "." in self.nombre else ""


@dataclass
class Asignacion:
    """Una compra con los archivos que le corresponden."""

    xml: Entrada
    pdfs: list[Entrada] = field(default_factory=list)
    cdr: Entrada | None = None


@dataclass
class Agrupacion:
    asignaciones: list[Asignacion] = field(default_factory=list)
    ignorados: list[tuple[Entrada, str, str]] = field(
        default_factory=list
    )  # (archivo, código, texto)


def raiz_xml(contenido: bytes) -> str | None:
    """Nombre local del elemento raíz, o `None` si no es un XML legible."""
    try:
        return etree.QName(etree.fromstring(contenido, _PARSER_SEGURO).tag).localname
    except (etree.XMLSyntaxError, ValueError):
        return None


def referencia_cdr(contenido: bytes) -> tuple[str, int] | None:
    """`(serie, correlativo)` del comprobante al que responde un CDR en XML."""
    try:
        root = etree.fromstring(contenido, _PARSER_SEGURO)
    except etree.XMLSyntaxError:
        return None
    for elemento in root.iter():
        if isinstance(elemento.tag, str) and etree.QName(elemento.tag).localname == "ReferenceID":
            return serie_numero_de(elemento.text or "")
    return None


def serie_numero_de(texto: str) -> tuple[str, int] | None:
    coincidencia = _SERIE_NUMERO.search(texto.upper())
    return (coincidencia.group(1), int(coincidencia.group(2))) if coincidencia else None


def _nombre_sin_extension(nombre: str) -> str:
    return nombre.rsplit(".", 1)[0].lower()


def agrupar(entradas: list[Entrada]) -> Agrupacion:
    """Reparte los archivos entre las compras (por carpeta) y separa lo que no se puede usar."""
    resultado = Agrupacion()
    carpetas: dict[str, list[Entrada]] = {}
    for entrada in entradas:
        carpetas.setdefault(entrada.carpeta, []).append(entrada)

    for archivos in carpetas.values():
        comprobantes: list[Entrada] = []
        cdrs: list[Entrada] = []
        pdfs: list[Entrada] = []
        for archivo in archivos:
            if archivo.extension == "pdf":
                pdfs.append(archivo)
            elif archivo.extension == "xml":
                raiz = raiz_xml(archivo.contenido)
                if raiz in _RAICES_COMPROBANTE:
                    comprobantes.append(archivo)
                elif raiz == "ApplicationResponse":
                    cdrs.append(archivo)
                elif raiz == "DespatchAdvice":
                    resultado.ignorados.append(
                        (archivo, "guia", "Guía de remisión: no se registra como compra")
                    )
                else:
                    resultado.ignorados.append(
                        (archivo, "xml_no_reconocido", "El XML no es un comprobante ni un CDR")
                    )
            else:
                resultado.ignorados.append((archivo, "tipo_no_soportado", "Solo XML y PDF"))
        _repartir(comprobantes, pdfs, cdrs, resultado)
    return resultado


def _repartir(
    comprobantes: list[Entrada], pdfs: list[Entrada], cdrs: list[Entrada], salida: Agrupacion
) -> None:
    if len(comprobantes) == 1:
        asignacion = Asignacion(xml=comprobantes[0], pdfs=list(pdfs))
        referencia = serie_numero_de(comprobantes[0].nombre)
        asignacion.cdr = next(
            (
                c
                for c in cdrs
                if referencia is None or referencia_cdr(c.contenido) in (None, referencia)
            ),
            cdrs[0] if cdrs else None,
        )
        salida.asignaciones.append(asignacion)
        for sobrante in cdrs:
            if sobrante is not asignacion.cdr:
                salida.ignorados.append((sobrante, "cdr_repetido", "Ya hay otro CDR en la carpeta"))
        return

    asignaciones = {c.ruta: Asignacion(xml=c) for c in comprobantes}
    claves = {c.ruta: _clave_de_comprobante(c) for c in comprobantes}
    for pdf in pdfs:
        destino = _comprobante_del_pdf(pdf, comprobantes, claves)
        if destino is None:
            salida.ignorados.append(
                (pdf, "sin_xml", "El PDF no se puede unir a un comprobante de la carpeta")
            )
        else:
            asignaciones[destino.ruta].pdfs.append(pdf)
    for cdr in cdrs:
        referencia = referencia_cdr(cdr.contenido)
        destino = next(
            (c for c in comprobantes if referencia and claves[c.ruta] == referencia), None
        )
        if destino is None or asignaciones[destino.ruta].cdr is not None:
            salida.ignorados.append(
                (cdr, "cdr_sin_comprobante", "El CDR no corresponde a un comprobante de la carpeta")
            )
        else:
            asignaciones[destino.ruta].cdr = cdr
    salida.asignaciones.extend(asignaciones.values())


def _clave_de_comprobante(xml: Entrada) -> tuple[str, int] | None:
    try:
        dato = parse_comprobante_ubl(xml.contenido)
    except AppError:
        return None
    return dato.serie, dato.correlativo


def _comprobante_del_pdf(
    pdf: Entrada, comprobantes: list[Entrada], claves: dict[str, tuple[str, int] | None]
) -> Entrada | None:
    base = _nombre_sin_extension(pdf.nombre)
    for c in comprobantes:  # mismo nombre que el XML
        if _nombre_sin_extension(c.nombre) == base:
            return c
    referencia = serie_numero_de(pdf.nombre)  # o la serie-número que el nombre lleve
    candidatos = [c for c in comprobantes if referencia and claves[c.ruta] == referencia]
    return candidatos[0] if len(candidatos) == 1 else None


# --------------------------------------------------------------------------- #
# Servicio
# --------------------------------------------------------------------------- #
def _leer_zip(paquete: bytes) -> list[Entrada]:
    if not paquete:
        raise InvalidDocumentError("El paquete está vacío")
    if not paquete.startswith(b"PK"):
        raise InvalidDocumentError("El archivo no es un paquete ZIP")
    entradas: list[Entrada] = []
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(paquete)) as archivo_zip:
            for info in archivo_zip.infolist():
                nombre = PurePosixPath(info.filename.replace("\\", "/"))
                if info.is_dir() or not nombre.name or nombre.name.startswith("."):
                    continue
                if info.file_size > MAX_BYTES_POR_ENTRADA:
                    raise FileTooLargeError(
                        details={"archivo": nombre.name, "maximo_bytes": MAX_BYTES_POR_ENTRADA}
                    )
                total += info.file_size
                if total > MAX_BYTES_DESCOMPRIMIDOS:
                    raise FileTooLargeError(
                        details={"maximo_bytes_descomprimidos": MAX_BYTES_DESCOMPRIMIDOS}
                    )
                # Solo la ruta relativa dentro del ZIP: nada de `..` ni rutas absolutas.
                partes = [p for p in nombre.parts if p not in ("/", "..", ".")]
                entradas.append(Entrada("/".join(partes), archivo_zip.read(info)))
    except zipfile.BadZipFile as exc:
        raise InvalidDocumentError("El paquete ZIP está dañado o incompleto") from exc
    if len(entradas) > MAX_ARCHIVOS_PAQUETE:
        raise FileTooLargeError(
            details={"maximo_archivos": MAX_ARCHIVOS_PAQUETE, "recibidos": len(entradas)}
        )
    return entradas


async def importar_paquete_proveedores(
    *,
    uow: UnitOfWork,
    settings: Settings,
    user: AuthenticatedUser,
    access: CompanyMember,
    paquete: bytes,
) -> PaqueteOut:
    """Importa las compras del ZIP (XML + PDF + CDR por carpeta) y devuelve el resultado."""
    entradas = _leer_zip(paquete)
    if not any(e.extension == "xml" for e in entradas):
        raise InvalidDocumentError("El paquete no contiene ningún archivo XML")
    agrupacion = agrupar(entradas)

    items = [
        await _importar(a, uow=uow, settings=settings, user=user, access=access)
        for a in agrupacion.asignaciones
    ]
    items.extend(
        PaqueteItemOut(archivo=e.nombre, ok=False, code=codigo, mensaje=texto)
        for e, codigo, texto in agrupacion.ignorados
    )
    informativos = {"conflict", "sin_xml", "guia", "cdr_sin_comprobante", "cdr_repetido"}
    return PaqueteOut(
        ruc=access.company.ruc,
        origen=OrigenComprobante.recibido,
        total=len(items),
        importados=sum(1 for i in items if i.ok),
        con_pdf=sum(1 for i in items if i.pdf),
        con_cdr=sum(1 for i in items if i.cdr),
        duplicados=sum(1 for i in items if i.code == "conflict"),
        con_error=sum(1 for i in items if not i.ok and i.code not in informativos | _SILENCIOSOS),
        sin_xml=sum(1 for i in items if i.code == "sin_xml"),
        items=items,
        procesado_at=datetime.now(tz=UTC),
    )


# Lo que se ignora a propósito: se informa pero no cuenta como error del paquete.
_SILENCIOSOS = frozenset({"xml_no_reconocido", "tipo_no_soportado"})


async def _importar(
    asignacion: Asignacion,
    *,
    uow: UnitOfWork,
    settings: Settings,
    user: AuthenticatedUser,
    access: CompanyMember,
) -> PaqueteItemOut:
    item = PaqueteItemOut(archivo=asignacion.xml.nombre)
    try:
        comprobante = await import_comprobante_xml(
            uow=uow,
            settings=settings,
            user=user,
            access=access,
            xml=asignacion.xml.contenido,
            origen=OrigenComprobante.recibido,
        )
        comprobante_id = comprobante.id
        numero = comprobante.nombre_archivo or comprobante.numero
    except AppError as exc:
        existente = (
            await _existente(uow, settings, access, asignacion) if exc.code == "conflict" else None
        )
        if existente is None:
            item.ok = False
            item.code = exc.code
            item.mensaje = redact(exc.message)
            return item
        # Ya estaba registrada con XML: se informa, pero se completa con el PDF/CDR que falten.
        item.code = "conflict"
        item.mensaje = "El comprobante ya estaba registrado"
        comprobante_id, numero = existente.id, existente.nombre_archivo or existente.numero

    item.comprobante_id = comprobante_id
    item.numero = numero
    archivados = await uow.archivos.list_for_comprobante(comprobante_id)

    for pdf in asignacion.pdfs:
        if any(a.tipo is TipoArchivo.pdf and a.nombre == pdf.nombre for a in archivados):
            item.pdf = True
            continue
        guardado = await guardar_archivo_sin_fallar(
            uow=uow,
            settings=settings,
            comprobante_id=comprobante_id,
            tipo=TipoArchivo.pdf,
            file_bytes=pdf.contenido,
            filename=pdf.nombre,
        )
        item.pdf = item.pdf or guardado is not None

    if asignacion.cdr is not None and not any(a.tipo is TipoArchivo.cdr for a in archivados):
        item.cdr = await _guardar_cdr(uow, settings, comprobante_id, asignacion.cdr)
    elif asignacion.cdr is not None:
        item.cdr = True
    return item


async def _existente(uow, settings, access, asignacion):
    try:
        dato = parse_comprobante_ubl(asignacion.xml.contenido)
    except AppError:
        return None
    return await uow.comprobantes.find_by_key(
        access.company_id,
        origen=OrigenComprobante.recibido,
        tipo_documento=dato.tipo_documento,
        serie=dato.serie,
        correlativo=dato.correlativo,
        sunat_ambiente=settings.effective_sunat_env(access.company.sunat_ambiente),
        ruc_emisor=dato.ruc_emisor,
    )


async def _guardar_cdr(uow, settings, comprobante_id, cdr_entrada) -> bool:
    try:
        cdr, zip_bytes = leer_cdr(cdr_entrada.contenido, "xml")
    except AppError:
        return False
    guardado = await guardar_archivo_sin_fallar(
        uow=uow,
        settings=settings,
        comprobante_id=comprobante_id,
        tipo=TipoArchivo.cdr,
        file_bytes=zip_bytes,
        filename=f"{_nombre_sin_extension(cdr_entrada.nombre)}.zip",
    )
    if guardado is None:
        return False  # sin archivo en R2 no hay evidencia: no se toca el estado
    comprobante = await uow.comprobantes.get(comprobante_id)
    if comprobante is not None:
        await uow.comprobantes.update_sunat_state(
            comprobante_id,
            estado=siguiente_estado(comprobante.estado, map_cdr_a_estado(cdr)),
            cdr_codigo=cdr.codigo,
            cdr_descripcion=cdr.descripcion,
            cdr_notas=cdr.notas,
        )
    return True
