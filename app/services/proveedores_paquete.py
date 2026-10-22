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
from uuid import UUID

from lxml import etree

from app.core.concurrency import run_blocking
from app.core.config import Settings
from app.core.enums import OrigenComprobante, SunatAmbiente, TipoArchivo
from app.core.errors import AppError, FileTooLargeError, InvalidDocumentError
from app.core.logging import get_logger
from app.core.redaction import redact
from app.core.security import AuthenticatedUser
from app.db.models import Archivo, CompanyMember, Comprobante
from app.db.repositories.base import UnitOfWork
from app.services.cdr_paquete import leer_cdr
from app.services.comprobantes import import_comprobante_xml, siguiente_estado
from app.services.pdf_identificar import candidatos_de_pdf
from app.services.results import PaqueteItemOut, PaqueteOut
from app.services.store import guardar_archivo_sin_fallar
from app.sunat.states import map_cdr_a_estado
from app.sunat.ubl import parse_comprobante_ubl

logger = get_logger(__name__)

TAMANO_MAXIMO_PAQUETE = 50 * 1024 * 1024
MAX_ARCHIVOS_PAQUETE = 400
MAX_ARCHIVOS_GRUPO = 60
MAX_BYTES_GRUPO = 60 * 1024 * 1024
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
# Situación de cada compra tras importar su XML (lo que se le muestra al usuario).
NUEVA = "nueva"  # no estaba en SIRE ni en la tabla: se creó con su XML
COMPLETADA = "completada"  # SIRE ya la traía: el XML le añadió ítems y archivo
REPETIDA = "repetida"  # ya tenía su XML

# Lo que se deja de lado a propósito: se informa, pero no cuenta como error del paquete.
_INFORMATIVOS = frozenset(
    {
        "conflict",
        "sin_xml",
        "guia",
        "cdr_sin_comprobante",
        "cdr_repetido",
        "xml_no_reconocido",
        "tipo_no_soportado",
        "sin_comprobante",
        "ambiguo",
        "xml_con_error",
    }
)


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
    if not any(e.extension in ("xml", "pdf") for e in entradas):
        raise InvalidDocumentError("El paquete no contiene ningún archivo XML ni PDF")
    return await procesar_entradas(entradas, uow=uow, settings=settings, user=user, access=access)


async def procesar_entradas(
    entradas: list[Entrada],
    *,
    uow: UnitOfWork,
    settings: Settings,
    user: AuthenticatedUser,
    access: CompanyMember,
) -> PaqueteOut:
    """Importa lo que reúnan `entradas` y devuelve UN resultado por archivo.

    Sirve igual para un ZIP completo que para un solo grupo de archivos: así el portal puede
    importar una compra tras otra y mostrar el avance en tiempo real.
    """
    agrupacion = agrupar(entradas)
    items: list[PaqueteItemOut] = []
    for asignacion in agrupacion.asignaciones:
        items.extend(
            await _importar(asignacion, uow=uow, settings=settings, user=user, access=access)
        )
    for archivo, codigo, texto in agrupacion.ignorados:
        if codigo == "sin_xml" and archivo.extension == "pdf":
            items.append(await _unir_pdf_suelto(archivo, uow=uow, settings=settings, access=access))
        else:
            items.append(
                PaqueteItemOut(
                    archivo=archivo.nombre,
                    ok=False,
                    tipo=archivo.extension or "otro",
                    situacion="omitido",
                    code=codigo,
                    mensaje=texto,
                )
            )

    xmls = [i for i in items if i.tipo == "xml"]
    return PaqueteOut(
        ruc=access.company.ruc,
        origen=OrigenComprobante.recibido,
        total=len(items),
        importados=sum(1 for i in xmls if i.ok and i.situacion in (NUEVA, COMPLETADA)),
        nuevas=sum(1 for i in xmls if i.ok and i.situacion == NUEVA),
        completadas=sum(1 for i in xmls if i.ok and i.situacion == COMPLETADA),
        con_pdf=sum(1 for i in items if i.tipo == "pdf" and i.ok),
        con_cdr=sum(1 for i in items if i.tipo == "cdr" and i.ok),
        duplicados=sum(1 for i in xmls if i.situacion == REPETIDA),
        con_error=sum(1 for i in items if not i.ok and i.code not in _INFORMATIVOS),
        sin_xml=sum(1 for i in items if i.code == "sin_xml"),
        items=items,
        procesado_at=datetime.now(tz=UTC),
    )


async def _previo(
    uow: UnitOfWork, settings: Settings, access: CompanyMember, xml: Entrada
) -> Comprobante | None:
    """La compra ya registrada con esa clave: en el ambiente del portal o, si es de SIRE, prod."""
    try:
        dato = parse_comprobante_ubl(xml.contenido)
    except AppError:
        return None
    ambiente = settings.effective_sunat_env(access.company.sunat_ambiente)
    for candidato in dict.fromkeys((ambiente, SunatAmbiente.prod)):
        existente = await uow.comprobantes.find_by_key(
            access.company_id,
            origen=OrigenComprobante.recibido,
            tipo_documento=dato.tipo_documento,
            serie=dato.serie,
            correlativo=dato.correlativo,
            sunat_ambiente=candidato,
            ruc_emisor=dato.ruc_emisor,
        )
        if existente is not None:
            return existente
    return None


async def _importar(
    asignacion: Asignacion,
    *,
    uow: UnitOfWork,
    settings: Settings,
    user: AuthenticatedUser,
    access: CompanyMember,
) -> list[PaqueteItemOut]:
    """Importa una compra y devuelve su XML, cada PDF y su CDR como resultados separados."""
    previo = await _previo(uow, settings, access, asignacion.xml)
    item = PaqueteItemOut(archivo=asignacion.xml.nombre, tipo="xml")
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
        if previo is not None and previo.fuente == "sire":
            item.situacion, item.mensaje = COMPLETADA, "Estaba en SIRE: se completó con su XML"
        else:
            item.situacion = NUEVA
            item.mensaje = "No estaba en SIRE ni en la tabla: se registró con su XML"
    except AppError as exc:
        if exc.code != "conflict" or previo is None:
            item.ok = False
            item.code = exc.code
            item.mensaje = redact(exc.message)
            sin_xml = [
                PaqueteItemOut(
                    archivo=a.nombre,
                    ok=False,
                    tipo=a.extension,
                    situacion="omitido",
                    code="xml_con_error",
                    mensaje="No se procesó porque el XML de la compra tiene un error",
                )
                for a in (*asignacion.pdfs, *([asignacion.cdr] if asignacion.cdr else []))
            ]
            return [item, *sin_xml]
        # Ya estaba registrada con XML: se informa, pero se completa con el PDF/CDR que falten.
        item.code, item.situacion = "conflict", REPETIDA
        item.mensaje = "Ya estaba registrada con su XML"
        comprobante_id = previo.id
        numero = previo.nombre_archivo or previo.numero

    item.comprobante_id = comprobante_id
    item.numero = numero
    archivados = await uow.archivos.list_for_comprobante(comprobante_id)
    resultados = [item]

    for pdf in asignacion.pdfs:
        resultados.append(
            await _guardar_pdf(uow, settings, comprobante_id, numero, pdf, archivados)
        )
    item.pdf = any(r.ok for r in resultados if r.tipo == "pdf")

    if asignacion.cdr is not None:
        resultado = await _guardar_cdr(
            uow, settings, comprobante_id, numero, asignacion.cdr, archivados
        )
        resultados.append(resultado)
        item.cdr = resultado.ok
    return resultados


async def _guardar_pdf(
    uow: UnitOfWork,
    settings: Settings,
    comprobante_id: UUID,
    numero: str,
    pdf: Entrada,
    archivados: list[Archivo],
    *,
    situacion: str = "guardado",
) -> PaqueteItemOut:
    item = PaqueteItemOut(
        archivo=pdf.nombre, tipo="pdf", comprobante_id=comprobante_id, numero=numero
    )
    if any(a.tipo is TipoArchivo.pdf and a.nombre == pdf.nombre for a in archivados):
        item.situacion, item.mensaje = "ya_estaba", "El PDF ya estaba archivado"
        return item
    guardado = await guardar_archivo_sin_fallar(
        uow=uow,
        settings=settings,
        comprobante_id=comprobante_id,
        tipo=TipoArchivo.pdf,
        file_bytes=pdf.contenido,
        filename=pdf.nombre,
    )
    if guardado is None:
        item.ok, item.code = False, "no_archivado"
        item.mensaje = "No se pudo guardar el PDF en el almacenamiento"
    else:
        item.situacion, item.mensaje = situacion, f"PDF guardado en {numero}"
    return item


async def _guardar_cdr(
    uow: UnitOfWork,
    settings: Settings,
    comprobante_id: UUID,
    numero: str,
    entrada: Entrada,
    archivados: list[Archivo],
) -> PaqueteItemOut:
    item = PaqueteItemOut(
        archivo=entrada.nombre, tipo="cdr", comprobante_id=comprobante_id, numero=numero
    )
    if any(a.tipo is TipoArchivo.cdr for a in archivados):
        item.situacion, item.mensaje = "ya_estaba", "El comprobante ya tenía su CDR"
        return item
    try:
        cdr, zip_bytes = leer_cdr(entrada.contenido, "xml")
    except AppError as exc:
        item.ok, item.code, item.mensaje = False, exc.code, redact(exc.message)
        return item
    guardado = await guardar_archivo_sin_fallar(
        uow=uow,
        settings=settings,
        comprobante_id=comprobante_id,
        tipo=TipoArchivo.cdr,
        file_bytes=zip_bytes,
        filename=f"{_nombre_sin_extension(entrada.nombre)}.zip",
    )
    if guardado is None:  # sin archivo en R2 no hay evidencia: no se toca el estado
        item.ok, item.code = False, "no_archivado"
        item.mensaje = "No se pudo guardar el CDR en el almacenamiento"
        return item
    comprobante = await uow.comprobantes.get(comprobante_id)
    if comprobante is not None:
        await uow.comprobantes.update_sunat_state(
            comprobante_id,
            estado=siguiente_estado(comprobante.estado, map_cdr_a_estado(cdr)),
            cdr_codigo=cdr.codigo,
            cdr_descripcion=cdr.descripcion,
            cdr_notas=cdr.notas,
        )
    item.situacion, item.mensaje = "guardado", f"CDR guardado en {numero}"
    return item


async def _unir_pdf_suelto(
    pdf: Entrada, *, uow: UnitOfWork, settings: Settings, access: CompanyMember
) -> PaqueteItemOut:
    """PDF sin XML: se une a una compra YA registrada si su texto da RUC y serie-número exactos."""
    rucs, series = await run_blocking(candidatos_de_pdf, pdf.contenido)
    encontradas: dict[UUID, Comprobante] = {}
    for ruc in rucs:
        for serie, correlativo in series:
            for c in await uow.comprobantes.find_recibidas(
                access.company_id, ruc_emisor=ruc, serie=serie, correlativo=correlativo
            ):
                encontradas[c.id] = c
    base = PaqueteItemOut(archivo=pdf.nombre, tipo="pdf", ok=False)
    if len(encontradas) != 1:
        base.situacion = "omitido"
        if len(encontradas) > 1:
            base.code = "ambiguo"
            base.mensaje = "El texto del PDF coincide con varias compras: no se unió"
        else:
            base.code = "sin_xml"
            base.mensaje = (
                "PDF sin XML: no se pudo identificar la compra por su texto "
                "(puede ser un escaneo o no estar registrada). Pide el XML al proveedor."
            )
        return base
    (compra,) = encontradas.values()
    archivados = await uow.archivos.list_for_comprobante(compra.id)
    numero = compra.nombre_archivo or compra.numero
    return await _guardar_pdf(uow, settings, compra.id, numero, pdf, archivados, situacion="unido")


async def importar_archivos_proveedores(
    *,
    uow: UnitOfWork,
    settings: Settings,
    user: AuthenticatedUser,
    access: CompanyMember,
    archivos: list[tuple[str, bytes]],
) -> PaqueteOut:
    """Importa un grupo de archivos sueltos (normalmente UNA compra con su PDF y su CDR).

    El portal los manda de a uno por compra para poder mostrar el avance en tiempo real: cada
    llamada se confirma sola, así que un error en una compra no deshace las anteriores.
    """
    if not archivos:
        raise InvalidDocumentError("No se envió ningún archivo")
    if len(archivos) > MAX_ARCHIVOS_GRUPO:
        raise FileTooLargeError(
            details={"maximo_archivos": MAX_ARCHIVOS_GRUPO, "recibidos": len(archivos)}
        )
    entradas: list[Entrada] = []
    usados: set[str] = set()
    for nombre, contenido in archivos:
        base = PurePosixPath(nombre.replace("\\", "/")).name or "archivo"
        unico, contador = base, 2
        while unico.lower() in usados:  # dos archivos con el mismo nombre en el grupo
            stem, punto, ext = base.rpartition(".")
            unico = f"{stem} ({contador}).{ext}" if punto else f"{base} ({contador})"
            contador += 1
        usados.add(unico.lower())
        entradas.append(Entrada(unico, contenido))
    return await procesar_entradas(entradas, uow=uow, settings=settings, user=user, access=access)
