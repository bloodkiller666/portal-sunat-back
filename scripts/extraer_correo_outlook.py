"""Extrae las facturas de proveedores de un buzón de Outlook clásico y las ordena por carpetas.

SOLO LECTURA sobre el buzón: no borra, mueve ni marca ningún correo. Cada comprobante queda en

    {destino}/AAAA/MES/RUC/SERIE-NÚMERO/        XML y PDF de ESA factura + `correo.txt`
        (AAAA/MES = fecha de emisión, con el mes en letras; RUC = el del proveedor; serie-número
        de 8 dígitos)

y lo que no se puede ubicar se aparta para revisarlo a mano:

    {destino}/_otras_empresas/{RUC adquirente}/AAAA/MES/RUC/SERIE-NÚMERO/    facturas a otro RUC
    {destino}/_sin_clasificar/AAAA-MES/{correo}/        PDF sin XML, fotos de documentos…
    {destino}/_correos/AAAA-MES/{correo}.json        de qué correo salió cada archivo (auditoría)
    {destino}/_correos/AAAA-MES/{correo}.msg         el correo original (prueba si lo borran)
    {destino}/_paquetes_importar/{fecha}/lote-NNN.zip    ZIP para el botón «Importar» (Compras)
    {destino}/registro_correos.jsonl, indice_comprobantes.jsonl, informe.json

La carpeta de cada compra sale del XML (proveedor, serie y número, fecha de emisión), no del
correo. Un PDF sin XML no dice a qué documento pertenece, por eso va a `_sin_clasificar`. Las
reglas para unir el PDF a su factura son las mismas del importador del backend
(`app.services.proveedores_paquete`). Al final de cada ejecución se intenta unir los PDF con
serie-número en el nombre que llegaron en otro correo.

* **CDR**: el portal ya no los guarda. Si llegan en el correo se descartan y se cuentan en el
  informe (`omitidos.cdr_ignorado`).
* **Asunto**: cada carpeta lleva `correo.txt` con el asunto, el remitente, la fecha, los adjuntos
  (con su SHA-256) y el cuerpo del correo del que salió: es el argumento de dónde vino. Si la misma
  factura llega en dos correos, queda un `correo.txt` por cada uno. No se importa al portal.
* **Fotos de documentos**: una imagen que no es firma ni logo (tamaño, nombre y si está incrustada
  en el cuerpo) se conserva. Como no se puede leer, va con la factura si el correo trae una sola
  y, si no, a `_sin_clasificar` (código `imagen`) para revisarla a mano.

Requisitos: Windows, Outlook CLÁSICO abierto con el buzón agregado y `pywin32`:

    uv run --with pywin32 python -m scripts.extraer_correo_outlook --buzon recepcionfacturas \\
        --desde 2026-10-01 --hasta 2026-10-31                    # prueba de un mes
    uv run --with pywin32 python -m scripts.extraer_correo_outlook --buzon recepcionfacturas
    uv run --with pywin32 python -m scripts.extraer_correo_outlook --buzon recepcionfacturas \\
        --simular

Es idempotente: un correo ya registrado no se vuelve a descargar y un archivo idéntico no se
duplica dentro de su carpeta.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from app.core.errors import AppError
from app.core.meses import nombre_mes, periodo_carpeta
from app.services.pdf_identificar import candidatos_de_pdf
from app.services.proveedores_paquete import (
    Asignacion,
    Entrada,
    agrupar,
    serie_numero_de,
)
from app.sunat.ubl import parse_comprobante_ubl

OL_MAIL = 43  # OlObjectClass.olMail
OL_BY_VALUE = 1  # OlAttachmentType.olByValue: archivo real
GUARDAR = {"xml", "pdf"}  # se guardan tal cual
ABRIR = {"zip"}  # se abren; el original se conserva
APARTE = {"rar", "7z", ""}  # se apartan para revisión manual (sin extensión incluida)
IMAGENES = {"png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp", "heic"}
DE_FIRMA = {"ico", "emf", "wmf", "svg"}  # nunca son un documento
# Una imagen más pequeña que esto es casi seguro un logo o el pie de una firma.
MIN_BYTES_FOTO = 40 * 1024
# Una imagen incrustada en el cuerpo (cid:) solo cuenta como documento si es grande.
MIN_BYTES_FOTO_INCRUSTADA = 400 * 1024
_NOMBRE_DE_FIRMA = re.compile(
    r"^(image\d{3}|outlook-[\w-]+|logo|firma|signature|banner|facebook|linkedin|twitter|"
    r"instagram|icon)",
    re.IGNORECASE,
)
_NOMBRE_DE_DOCUMENTO = re.compile(
    r"factura|boleta|guia|guía|remision|remisión|comprobante|documento|scan|escane|foto|img[_-]",
    re.IGNORECASE,
)
MAX_CUERPO_CORREO = 20_000  # caracteres del cuerpo que se guardan en `correo.txt`
NOMBRE_CORREO = "correo.txt"

CARPETAS_POR_DEFECTO = "Bandeja de entrada,MEGA GAS,TEMPO TOURS,TRANSPORTES CIMSA"
RUC_EMPRESA_POR_DEFECTO = "20514831620"
# Topes contra ZIP maliciosos o corruptos (el contenido viene de correos externos).
MAX_ENTRADAS_ZIP = 200
MAX_BYTES_ENTRADA = 50 * 1024 * 1024
MAX_BYTES_ZIP_TOTAL = 300 * 1024 * 1024
MAX_BYTES_ADJUNTO = 100 * 1024 * 1024
# Cada lote de importación debe caber en los topes del backend (≤ 400 archivos, ≤ 50 MB).
MAX_ARCHIVOS_LOTE = 300
MAX_BYTES_LOTE = 35 * 1024 * 1024

_NO_PERMITIDO = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


# --------------------------------------------------------------------------- #
# Lógica pura (se prueba sin Outlook)
# --------------------------------------------------------------------------- #
def extension(nombre: str) -> str:
    return nombre.rsplit(".", 1)[-1].lower() if "." in nombre else ""


def nombre_seguro(nombre: str, vacio: str = "archivo") -> str:
    """Nombre de archivo válido en Windows, sin rutas ni caracteres reservados."""
    base = PurePosixPath(nombre.replace("\\", "/")).name
    limpio = _NO_PERMITIDO.sub("-", base).strip(" .")
    return limpio[:150] or vacio


def id_correo(entry_id: str) -> str:
    return hashlib.sha1(entry_id.encode()).hexdigest()[:12]


def carpeta_de_correo(destino: Path, recibido: datetime, identificador: str) -> Path:
    return destino / periodo_carpeta(recibido) / f"{recibido:%Y%m%d-%H%M%S}_{identificador[:6]}"


def nombre_libre(carpeta: Path, nombre: str) -> Path:
    """Ruta que no pisa un archivo existente (`a.pdf`, `a (2).pdf`…)."""
    ruta = carpeta / nombre
    if not ruta.exists():
        return ruta
    stem, punto, ext = nombre.rpartition(".")
    base, sufijo = (stem, f".{ext}") if punto else (nombre, "")
    contador = 2
    while (carpeta / f"{base} ({contador}){sufijo}").exists():
        contador += 1
    return carpeta / f"{base} ({contador}){sufijo}"


def leer_zip(contenido: bytes) -> tuple[list[tuple[str, bytes]], list[str]]:
    """XML, PDF y fotos de un ZIP, con topes de tamaño. Devuelve `(archivos, motivos_de_omision)`.

    Solo se usa el nombre base de cada entrada (jamás una ruta del ZIP) y no se abren ZIP
    anidados. Un ZIP ilegible o que supera los topes se informa, no detiene la extracción.
    """
    archivos: list[tuple[str, bytes]] = []
    omitidos: list[str] = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(contenido))
    except zipfile.BadZipFile:
        return [], ["zip_invalido"]
    entradas = [info for info in zf.infolist() if not info.is_dir()]
    if len(entradas) > MAX_ENTRADAS_ZIP:
        return [], ["zip_con_demasiados_archivos"]
    total = 0
    for info in entradas:
        ext = extension(info.filename)
        foto = ext in IMAGENES and es_foto_de_documento(info.filename, info.file_size, False)
        if ext not in GUARDAR and not foto:
            omitidos.append(f"zip_omite_{ext or 'sin_extension'}")
            continue
        with zf.open(info) as flujo:
            datos = flujo.read(MAX_BYTES_ENTRADA + 1)  # tope real, no el tamaño declarado
        total += len(datos)
        if len(datos) > MAX_BYTES_ENTRADA or total > MAX_BYTES_ZIP_TOTAL:
            omitidos.append("zip_supera_el_tamano_maximo")
            break
        archivos.append((nombre_seguro(info.filename), datos))
    return archivos, omitidos


def sha256(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


@dataclass(frozen=True)
class Colocacion:
    """Un archivo y la carpeta (relativa al destino, con `/`) en la que debe quedar."""

    ruta: str
    datos: bytes
    origen: str = "adjunto"


def es_foto_de_documento(nombre: str, tamano: int, incrustada: bool) -> bool:
    """¿Esta imagen adjunta puede ser la foto de un documento (y no un logo o una firma)?

    Se prefiere conservar de más: una foto de factura descartada se pierde sin avisar, mientras
    que un logo de más solo ocupa lugar en `_sin_clasificar`.
    """
    if extension(nombre) in DE_FIRMA:
        return False
    if incrustada:  # pegada en el cuerpo del correo: casi siempre firma, salvo que sea grande
        return tamano >= MIN_BYTES_FOTO_INCRUSTADA
    if _NOMBRE_DE_DOCUMENTO.search(nombre):
        return tamano >= 5 * 1024
    if _NOMBRE_DE_FIRMA.search(PurePosixPath(nombre).name):
        return tamano >= MIN_BYTES_FOTO_INCRUSTADA
    return tamano >= MIN_BYTES_FOTO


@dataclass(frozen=True)
class MetaCorreo:
    """Del correo, lo que queda como argumento de dónde salieron los archivos."""

    asunto: str
    remitente: str
    para: str
    recibido: datetime
    carpeta_outlook: str
    cuerpo: str = ""


def texto_correo(meta: MetaCorreo, archivos: list[tuple[str, int, str]]) -> str:
    """`correo.txt`: asunto, remitente, fecha, adjuntos (nombre, bytes, SHA-256) y cuerpo."""
    cuerpo = meta.cuerpo.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(cuerpo) > MAX_CUERPO_CORREO:
        cuerpo = cuerpo[:MAX_CUERPO_CORREO] + "\n[… cuerpo recortado]"
    lineas = [
        f"Asunto:    {meta.asunto or '(sin asunto)'}",
        f"De:        {meta.remitente or '(desconocido)'}",
        f"Para:      {meta.para or '(desconocido)'}",
        f"Recibido:  {meta.recibido:%Y-%m-%d %H:%M:%S}",
        f"Carpeta:   {meta.carpeta_outlook}",
        "",
        "Archivos de este correo:",
        *[f"  - {nombre} ({tam} bytes, sha256 {huella[:16]}…)" for nombre, tam, huella in archivos],
        "",
        "Cuerpo:",
        cuerpo or "(vacío)",
    ]
    return "\n".join(lineas) + "\n"


@dataclass
class ComprobanteColocado:
    serie: str
    correlativo: int
    ruc_emisor: str
    carpeta: str  # relativa al destino
    empresa: bool  # True si es compra de la empresa; False si es de otro RUC del grupo


@dataclass
class PlanCorreo:
    colocaciones: list[Colocacion] = field(default_factory=list)
    comprobantes: list[ComprobanteColocado] = field(default_factory=list)
    sin_clasificar: list[tuple[str, str]] = field(default_factory=list)  # (nombre, motivo)
    omitidos: Counter[str] = field(default_factory=Counter)  # p. ej. `cdr_ignorado`


def carpeta_de_comprobante(
    ruc_emisor: str,
    serie: str,
    correlativo: int,
    emision: date | None,
    adquirente: str | None,
    ruc_empresa: str,
) -> str:
    """`AAAA/MES/RUC/SERIE-NÚMERO` (o bajo `_otras_empresas/{RUC}` si compra otro RUC)."""
    anio, mes = ("sin-fecha",) * 2
    if emision:
        anio, mes = f"{emision.year:04d}", nombre_mes(emision.month)
    base = f"{anio}/{mes}/{ruc_emisor}/{serie}-{correlativo:08d}"
    if adquirente and adquirente != ruc_empresa:
        return f"_otras_empresas/{adquirente}/{base}"
    return base


def planificar_correo(
    archivos: list[tuple[str, bytes, str]],
    *,
    ruc_empresa: str,
    recibido: datetime,
    identificador: str,
    meta: MetaCorreo | None = None,
) -> PlanCorreo:
    """Decide dónde va cada archivo de UN correo. `archivos` = `(nombre, datos, origen)`.

    Con `meta`, además deja `correo.txt` (asunto, remitente…) en cada carpeta donde pone algo.
    """
    plan = PlanCorreo()
    origenes: dict[str, str] = {}
    entradas: list[Entrada] = []
    usados: set[str] = set()
    for nombre, datos, origen in archivos:
        if origen == "zip_original":
            # El ZIP se conserva aparte: su contenido ya se reparte con los demás adjuntos.
            ruta = (
                f"_originales_zip/{periodo_carpeta(recibido)}/"
                f"{recibido:%Y%m%d-%H%M%S}_{identificador[:6]}"
            )
            plan.colocaciones.append(Colocacion(f"{ruta}/{nombre}", datos, origen))
            continue
        unico, contador = nombre, 2
        while unico.lower() in usados:  # dos adjuntos con el mismo nombre en un correo
            stem, punto, ext = nombre.rpartition(".")
            unico = f"{stem} ({contador}).{ext}" if punto else f"{nombre} ({contador})"
            contador += 1
        usados.add(unico.lower())
        origenes[unico] = origen
        entradas.append(Entrada(unico, datos))

    agrupacion = agrupar(entradas)
    destino_unico: str | None = None
    destinos: list[tuple[str, str, str, int]] = []  # (carpeta, RUC, serie, número) de cada factura
    for asignacion in agrupacion.asignaciones:
        destino = _carpeta_de(asignacion, ruc_empresa, plan)
        if destino is not None:
            destino_unico = destino if len(agrupacion.asignaciones) == 1 else None
            c = plan.comprobantes[-1]
            destinos.append((destino, c.ruc_emisor, c.serie, c.correlativo))
        for archivo in _archivos_de(asignacion):
            ruta = destino if destino is not None else _sin_clasificar(recibido, identificador)
            plan.colocaciones.append(
                Colocacion(f"{ruta}/{archivo.nombre}", archivo.contenido, origenes[archivo.nombre])
            )
            if destino is None:
                plan.sin_clasificar.append((archivo.nombre, "xml_no_valido"))

    for archivo, codigo, _texto in agrupacion.ignorados:
        if codigo == "cdr_ignorado":
            plan.omitidos[codigo] += 1  # el portal no guarda CDR
            continue
        if codigo == "tipo_no_soportado":
            # Una foto de factura o de guía se reconoce por su extensión; el resto (RAR, archivos
            # sin extensión…) se aparta para revisarlo a mano.
            codigo = "imagen" if archivo.extension in IMAGENES else "revisar_a_mano"
        if codigo == "sin_xml" and archivo.extension == "pdf" and destinos:
            # PDF que no es el de una factura (guía de remisión, nota de crédito, orden…): se
            # conserva TAL CUAL, sin renombrarlo ni mezclarlo, en la factura a la que se refiere
            # o, si no se sabe cuál, en todas las del correo para que llegue a Cloudflare y al
            # portal.
            for carpeta in _carpetas_del_pdf(archivo.contenido, destinos):
                plan.colocaciones.append(
                    Colocacion(
                        f"{carpeta}/{archivo.nombre}", archivo.contenido, origenes[archivo.nombre]
                    )
                )
            continue
        # Con una sola factura en el correo, su guía de remisión o su foto van con ella.
        ruta = destino_unico if codigo in ("guia", "imagen") and destino_unico else None
        ruta = ruta or _sin_clasificar(recibido, identificador)
        if ruta.startswith("_sin_clasificar"):
            plan.sin_clasificar.append((archivo.nombre, codigo))
        plan.colocaciones.append(
            Colocacion(f"{ruta}/{archivo.nombre}", archivo.contenido, origenes[archivo.nombre])
        )
    if meta is not None:
        _agregar_texto_correo(plan, meta)
    return plan


def _carpetas_del_pdf(contenido: bytes, destinos: list[tuple[str, str, str, int]]) -> list[str]:
    """Carpetas de factura donde va un PDF que el nombre no une a ninguna.

    Si su texto cita el RUC y la serie-número de una o más facturas del correo, va a esas. Si no
    (escaneo, guía sin la factura citada, orden de compra…), va a todas las del correo: nada se
    pierde y cada factura conserva el adjunto completo de su correo.
    """
    rucs, series = candidatos_de_pdf(contenido)
    citadas = [d for d in destinos if d[1] in rucs and (d[2].upper(), d[3]) in series]
    return list(dict.fromkeys(d[0] for d in (citadas or destinos)))


def _agregar_texto_correo(plan: PlanCorreo, meta: MetaCorreo) -> None:
    """Un `correo.txt` por carpeta de destino (no por el ZIP original ni por `_originales_zip`)."""
    carpetas = dict.fromkeys(
        c.ruta.rpartition("/")[0]
        for c in plan.colocaciones
        if not c.ruta.startswith("_originales_zip/")
    )
    for carpeta in carpetas:
        presentes = [
            (c.ruta.rpartition("/")[2], len(c.datos), sha256(c.datos))
            for c in plan.colocaciones
            if c.ruta.rpartition("/")[0] == carpeta
        ]
        plan.colocaciones.append(
            Colocacion(
                f"{carpeta}/{NOMBRE_CORREO}",
                texto_correo(meta, presentes).encode("utf-8"),
                "correo",
            )
        )


def _sin_clasificar(recibido: datetime, identificador: str) -> str:
    return (
        f"_sin_clasificar/{periodo_carpeta(recibido)}/{recibido:%Y%m%d-%H%M%S}_{identificador[:6]}"
    )


def _archivos_de(asignacion: Asignacion) -> list[Entrada]:
    return [asignacion.xml, *asignacion.pdfs]


def _carpeta_de(asignacion: Asignacion, ruc_empresa: str, plan: PlanCorreo) -> str | None:
    try:
        dato = parse_comprobante_ubl(asignacion.xml.contenido)
    except AppError:
        return None
    carpeta = carpeta_de_comprobante(
        dato.ruc_emisor,
        dato.serie,
        dato.correlativo,
        dato.fecha_emision,
        dato.ruc_receptor,
        ruc_empresa,
    )
    plan.comprobantes.append(
        ComprobanteColocado(
            dato.serie, dato.correlativo, dato.ruc_emisor, carpeta, not carpeta.startswith("_otras")
        )
    )
    return carpeta


def trozos_para_lotes(
    carpetas: list[tuple[str, list[tuple[str, int]]]],
    *,
    max_archivos: int = MAX_ARCHIVOS_LOTE,
    max_bytes: int = MAX_BYTES_LOTE,
) -> list[list[str]]:
    """Reparte carpetas completas en lotes que no superen los topes del importador.

    `carpetas` = `(ruta, [(archivo, bytes)…])`. Una carpeta nunca se parte entre dos lotes:
    el PDF debe viajar con su XML.
    """
    lotes: list[list[str]] = []
    actual: list[str] = []
    cuenta = peso = 0
    for ruta, contenido in carpetas:
        n, b = len(contenido), sum(tam for _, tam in contenido)
        if actual and (cuenta + n > max_archivos or peso + b > max_bytes):
            lotes.append(actual)
            actual, cuenta, peso = [], 0, 0
        actual.append(ruta)
        cuenta += n
        peso += b
    if actual:
        lotes.append(actual)
    return lotes


def anotar_pendientes(destino: Path, tocadas: set[str]) -> None:
    """Suma las carpetas con novedades a la lista de lo que falta importar al portal.

    Las compras (`AAAA/MM/RUC/SERIE-NÚMERO`) se importan completas; las de `_sin_clasificar` se
    revisan solo para unir sus PDF por el texto. El importador quita de la lista lo que sale bien,
    así que lo que falle se reintenta solo en la corrida siguiente.
    """
    if not tocadas:
        return
    ruta = destino / "_automatizacion" / "pendientes_importar.json"
    ruta.parent.mkdir(parents=True, exist_ok=True)
    actual = json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else {}
    carpetas = set(actual.get("carpetas", []))
    sueltas = set(actual.get("sin_clasificar", []))
    for carpeta in tocadas:
        if carpeta.startswith("_sin_clasificar/"):
            sueltas.add(carpeta)
        elif carpeta[:4].isdigit():  # AAAA/MM/RUC/SERIE-NÚMERO de la empresa
            carpetas.add(carpeta)
    ruta.write_text(
        json.dumps(
            {"carpetas": sorted(carpetas), "sin_clasificar": sorted(sueltas)},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


@dataclass
class Informe:
    revisados: int = 0
    ya_registrados: int = 0
    fuera_de_fechas: int = 0
    con_facturas: int = 0
    sin_facturas: int = 0
    archivos: Counter[str] = field(default_factory=Counter)
    omitidos: Counter[str] = field(default_factory=Counter)
    comprobantes: int = 0
    otras_empresas: int = 0
    sin_clasificar: Counter[str] = field(default_factory=Counter)
    ya_existian: int = 0
    unidos_despues: Counter[str] = field(default_factory=Counter)
    errores: int = 0
    bytes_guardados: int = 0


# --------------------------------------------------------------------------- #
# Disco
# --------------------------------------------------------------------------- #
def guardar_sin_duplicar(carpeta: Path, nombre: str, datos: bytes) -> bool:
    """Guarda el archivo salvo que la carpeta ya tenga uno idéntico. Devuelve si escribió."""
    if carpeta.is_dir():
        huella = sha256(datos)
        for existente in carpeta.iterdir():
            if existente.is_file() and sha256(existente.read_bytes()) == huella:
                return False
    carpeta.mkdir(parents=True, exist_ok=True)
    nombre_libre(carpeta, nombre_seguro(nombre)).write_bytes(datos)
    return True


def _escribir(
    destino: Path, plan: PlanCorreo, informe: Informe, tocadas: set[str] | None = None
) -> int:
    """Escribe el plan en disco. Devuelve cuántos archivos eran nuevos.

    `tocadas` recoge las carpetas (relativas al destino) donde llegó algo nuevo: son las que hay
    que importar después.
    """
    nuevos = 0
    for c in plan.colocaciones:
        carpeta, _, nombre = c.ruta.rpartition("/")
        if guardar_sin_duplicar(destino / carpeta, nombre, c.datos):
            nuevos += 1
            # El `correo.txt` es solo evidencia: no hay nada que importar por él.
            if tocadas is not None and c.origen != "correo":
                tocadas.add(carpeta)
            informe.bytes_guardados += len(c.datos)
            informe.archivos[extension(nombre) or "(sin extensión)"] += 1
    return nuevos


def _indice_cargar(ruta: Path) -> dict[tuple[str, int], list[dict[str, Any]]]:
    indice: dict[tuple[str, int], list[dict[str, Any]]] = {}
    if ruta.exists():
        for linea in ruta.read_text(encoding="utf-8").splitlines():
            if linea.strip():
                fila = json.loads(linea)
                indice.setdefault((fila["serie"], fila["correlativo"]), []).append(fila)
    return indice


def unir_sueltos(
    destino: Path,
    indice: dict[tuple[str, int], list[dict[str, Any]]],
    tocadas: set[str] | None = None,
) -> Counter[str]:
    """Une el PDF que llegó en otro correo cuando su nombre lleva la serie-número de la compra.

    Solo se une cuando el destino es único (una sola compra con esa serie-número) y todavía no
    tiene PDF: ante la duda el archivo se queda en `_sin_clasificar`.
    """
    unidos: Counter[str] = Counter()
    base = destino / "_sin_clasificar"
    if not base.is_dir():
        return unidos
    for archivo in sorted(p for p in base.rglob("*.pdf") if p.is_file()):
        clave = serie_numero_de(archivo.name)
        candidatos = [f for f in indice.get(clave, []) if f["empresa"]] if clave else []
        if len(candidatos) != 1:
            continue
        carpeta = destino / candidatos[0]["carpeta"]
        if any(f.suffix.lower() == ".pdf" for f in carpeta.glob("*")):
            continue
        carpeta.mkdir(parents=True, exist_ok=True)
        shutil.move(str(archivo), str(nombre_libre(carpeta, archivo.name)))
        unidos["pdf"] += 1
        if tocadas is not None:
            tocadas.add(candidatos[0]["carpeta"])
    return unidos


def crear_paquetes(destino: Path, simular: bool) -> list[Path]:
    """ZIP con las carpetas de compras de la empresa, listos para el botón «Importar»."""
    carpetas: list[tuple[str, list[tuple[str, int]]]] = []
    for ruta in sorted(destino.glob("[0-9][0-9][0-9][0-9]/*/*/*")):
        if ruta.is_dir():
            archivos = [
                (f.name, f.stat().st_size)
                for f in ruta.iterdir()
                if f.is_file() and extension(f.name) in GUARDAR
            ]
            if archivos:
                carpetas.append((ruta.relative_to(destino).as_posix(), archivos))
    lotes = trozos_para_lotes(carpetas)
    if simular or not lotes:
        return []
    salida = destino / "_paquetes_importar" / f"{datetime.now():%Y%m%d-%H%M%S}"
    salida.mkdir(parents=True, exist_ok=True)
    creados: list[Path] = []
    for numero, lote in enumerate(lotes, start=1):
        ruta_zip = salida / f"lote-{numero:03d}.zip"
        with zipfile.ZipFile(ruta_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for carpeta in lote:
                for archivo in sorted((destino / carpeta).iterdir()):
                    if archivo.is_file() and extension(archivo.name) in GUARDAR:
                        zf.write(archivo, f"{carpeta}/{archivo.name}")
        creados.append(ruta_zip)
    return creados


# --------------------------------------------------------------------------- #
# Outlook (solo Windows)
# --------------------------------------------------------------------------- #
def _fecha(valor: Any) -> datetime | None:
    try:
        return datetime(valor.year, valor.month, valor.day, valor.hour, valor.minute, valor.second)
    except Exception:
        return None


def _carpetas(raiz: Any, nombres: set[str]) -> list[Any]:
    encontradas: list[Any] = []
    for carpeta in raiz.Folders:
        if str(carpeta.Name).lower() in nombres:
            encontradas.append(carpeta)
        encontradas.extend(_carpetas(carpeta, nombres))
    return encontradas


def _cargar_registro(ruta: Path) -> set[str]:
    if not ruta.exists():
        return set()
    return {
        json.loads(linea)["id"]
        for linea in ruta.read_text(encoding="utf-8").splitlines()
        if linea.strip()
    }


def _propiedad(objeto: Any, nombre: str) -> str:
    """Propiedad MAPI de un correo o adjunto (`0x5D01001F`…); vacío si Outlook no la da."""
    try:
        valor = objeto.PropertyAccessor.GetProperty(
            f"http://schemas.microsoft.com/mapi/proptag/{nombre}"
        )
    except Exception:
        return ""
    return str(valor or "")


def _es_incrustada(adjunto: Any) -> bool:
    """¿La imagen va pegada en el cuerpo (`cid:`)? Así viajan los logos y las firmas."""
    return bool(_propiedad(adjunto, "0x3712001F"))


def meta_de_correo(correo: Any, carpeta: Any, recibido: datetime) -> MetaCorreo:
    """Asunto, remitente y cuerpo del correo (mejor esfuerzo: Outlook puede no dar alguno)."""

    def texto(atributo: str) -> str:
        try:
            return str(getattr(correo, atributo, "") or "")
        except Exception:
            return ""

    # `SenderEmailAddress` de Exchange es una ruta interna; la dirección real está en MAPI.
    direccion = _propiedad(correo, "0x5D01001F") or texto("SenderEmailAddress")
    nombre = texto("SenderName")
    remitente = f"{nombre} <{direccion}>" if direccion and direccion != nombre else nombre
    return MetaCorreo(
        asunto=texto("Subject"),
        remitente=remitente,
        para=texto("To"),
        recibido=recibido,
        carpeta_outlook=str(carpeta.Name),
        cuerpo=texto("Body"),
    )


def _adjuntos_utiles(
    correo: Any, destino: Path, identificador: str, informe: Informe
) -> list[tuple[str, bytes, str]]:
    """Descarga los adjuntos útiles del correo: `(nombre, datos, origen)`. Abre los ZIP."""
    resultado: list[tuple[str, bytes, str]] = []
    adjuntos = correo.Attachments
    for indice in range(1, adjuntos.Count + 1):
        adjunto = adjuntos.Item(indice)
        if adjunto.Type != OL_BY_VALUE:
            continue
        original = str(adjunto.FileName)
        ext = extension(original)
        if ext in DE_FIRMA:
            continue
        if ext in IMAGENES and not es_foto_de_documento(
            original, int(adjunto.Size), _es_incrustada(adjunto)
        ):
            informe.omitidos["imagen_de_firma_o_logo"] += 1
            continue
        if ext not in GUARDAR | ABRIR | APARTE | IMAGENES:
            informe.omitidos[f"omite_{ext}"] += 1
            continue
        if int(adjunto.Size) > MAX_BYTES_ADJUNTO:
            informe.omitidos["adjunto_demasiado_grande"] += 1
            continue
        # Outlook solo sabe guardar a disco: se guarda en una carpeta temporal y se lee.
        temporal = destino / "_temporal" / f"{identificador}_{indice}"
        temporal.parent.mkdir(parents=True, exist_ok=True)
        adjunto.SaveAsFile(str(temporal))
        try:
            datos = temporal.read_bytes()
        finally:
            temporal.unlink(missing_ok=True)
        nombre = nombre_seguro(original)
        if ext in GUARDAR | IMAGENES:
            resultado.append((nombre, datos, "adjunto"))
        elif ext in ABRIR:
            contenidos, motivos = leer_zip(datos)
            for motivo in motivos:
                informe.omitidos[motivo] += 1
            resultado.extend((interno, bruto, f"zip:{nombre}") for interno, bruto in contenidos)
            resultado.append((f"{nombre}", datos, "zip_original"))
        else:
            resultado.append((nombre if ext else f"{nombre}.bin", datos, "revisar"))
    return resultado


def extraer(args: argparse.Namespace) -> tuple[int, dict[str, Any] | None]:
    """Extrae el correo nuevo. Devuelve `(código de salida, resumen)`; sin resumen si falló."""
    try:
        import win32com.client  # type: ignore[import-not-found]
    except ImportError:
        print(
            "Falta pywin32: ejecute con "
            "`uv run --with pywin32 python -m scripts.extraer_correo_outlook`"
        )
        return 2, None

    destino = Path(args.destino).expanduser().resolve()
    desde = date.fromisoformat(args.desde) if args.desde else None
    hasta = date.fromisoformat(args.hasta) if args.hasta else None
    nombres = {n.strip().lower() for n in args.carpetas.split(",") if n.strip()}

    espacio = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
    buzones = [b for b in espacio.Folders if args.buzon.lower() in str(b.Name).lower()]
    if len(buzones) != 1:
        print(f"Se esperaba 1 buzón con «{args.buzon}» y hay {len(buzones)}.")
        return 2, None
    carpetas = _carpetas(buzones[0], nombres)
    if not carpetas:
        print(f"No se encontró ninguna de estas carpetas: {sorted(nombres)}")
        return 2, None

    if not args.simular:
        destino.mkdir(parents=True, exist_ok=True)
    ruta_registro = destino / "registro_correos.jsonl"
    ruta_indice = destino / "indice_comprobantes.jsonl"
    ya_hechos = _cargar_registro(ruta_registro)
    indice = _indice_cargar(ruta_indice)
    informe = Informe()
    vistos_empresa: set[str] = set()
    vistos_otras: set[str] = set()
    tocadas: set[str] = set()
    print(
        f"Buzón: {buzones[0].Name} | destino: {destino} | "
        f"{'SIMULACIÓN' if args.simular else 'guardando'}"
    )

    for carpeta in carpetas:
        items = carpeta.Items
        total = items.Count
        print(f"Leyendo «{carpeta.Name}» ({total} elementos)…", file=sys.stderr, flush=True)
        for posicion in range(1, total + 1):
            if args.limite and informe.con_facturas >= args.limite:
                break
            if posicion % 200 == 0:
                print(f"    … {posicion}/{total}", file=sys.stderr, flush=True)
            try:
                correo = items.Item(posicion)
                if correo.Class != OL_MAIL:
                    continue
                recibido = _fecha(correo.ReceivedTime)
                if recibido is None:
                    continue
                informe.revisados += 1
                if (desde and recibido.date() < desde) or (hasta and recibido.date() > hasta):
                    informe.fuera_de_fechas += 1
                    continue
                identificador = id_correo(str(correo.EntryID))
                if identificador in ya_hechos:
                    informe.ya_registrados += 1
                    continue
                if correo.Attachments.Count == 0:
                    informe.sin_facturas += 1
                    continue
                archivos = _adjuntos_utiles(correo, destino, identificador, informe)
                if not archivos:
                    informe.sin_facturas += 1
                    continue
                plan = planificar_correo(
                    archivos,
                    ruc_empresa=args.ruc,
                    recibido=recibido,
                    identificador=identificador,
                    meta=meta_de_correo(correo, carpeta, recibido),
                )
                informe.con_facturas += 1
                informe.omitidos.update(plan.omitidos)
                for c in plan.comprobantes:  # sin repetir la factura que llegó en varios correos
                    (vistos_empresa if c.empresa else vistos_otras).add(c.carpeta)
                informe.sin_clasificar.update(motivo for _, motivo in plan.sin_clasificar)
                if args.simular:
                    continue
                meta = meta_de_correo(correo, carpeta, recibido)
                nuevos = _escribir(destino, plan, informe, tocadas)
                informe.ya_existian += len(plan.colocaciones) - nuevos
                _registrar(
                    destino,
                    ruta_registro,
                    ruta_indice,
                    indice,
                    correo,
                    carpeta,
                    recibido,
                    identificador,
                    plan,
                    meta,
                )
            except Exception as exc:
                informe.errores += 1
                print(f"    correo {posicion}: {type(exc).__name__}", file=sys.stderr)

    informe.comprobantes = len(vistos_empresa)
    informe.otras_empresas = len(vistos_otras)
    paquetes: list[Path] = []
    if not args.simular:
        informe.unidos_despues.update(unir_sueltos(destino, indice, tocadas))
        anotar_pendientes(destino, tocadas)
        temporal = destino / "_temporal"
        if temporal.is_dir():
            temporal.rmdir()  # queda vacía: Outlook solo guarda adjuntos a disco
        if not args.sin_paquetes:
            paquetes = crear_paquetes(destino, simular=False)

    resumen_final = {
        "revisados": informe.revisados,
        "con_facturas": informe.con_facturas,
        "sin_facturas": informe.sin_facturas,
        "ya_registrados": informe.ya_registrados,
        "fuera_de_fechas": informe.fuera_de_fechas,
        "comprobantes_de_la_empresa": informe.comprobantes,
        "comprobantes_de_otras_empresas": informe.otras_empresas,
        "sin_clasificar": dict(informe.sin_clasificar),
        "unidos_despues": dict(informe.unidos_despues),
        "archivos_nuevos": dict(informe.archivos),
        "archivos_repetidos_omitidos": informe.ya_existian,
        "omitidos": dict(informe.omitidos),
        "errores": informe.errores,
        "mb_guardados": round(informe.bytes_guardados / 1e6, 1),
        "paquetes_para_importar": [str(p) for p in paquetes],
        "simulacion": args.simular,
    }
    print(json.dumps(resumen_final, ensure_ascii=False, indent=2))
    if not args.simular:
        (destino / "informe.json").write_text(
            json.dumps(resumen_final, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0, resumen_final


def ejecutar(args: argparse.Namespace) -> int:
    return extraer(args)[0]


def _guardar_mensaje_original(correo: Any, ruta: Path, destino: Path) -> dict[str, Any] | None:
    """Guarda el correo tal cual (`.msg`, formato 3 de Outlook): la prueba de que sí llegó.

    Si luego lo borran del buzón, el `.msg` conserva cabeceras, cuerpo y adjuntos originales; su
    SHA-256 queda en el JSON de auditoría. Es de mejor esfuerzo: un fallo no impide registrar las
    facturas (devuelve `None` y el JSON conserva asunto, remitente y huellas de los adjuntos).
    """
    try:
        if not ruta.exists():
            correo.SaveAs(str(ruta), 3)
        datos = ruta.read_bytes()
    except Exception as exc:
        print(f"    no se pudo guardar el correo original: {type(exc).__name__}", file=sys.stderr)
        ruta.unlink(missing_ok=True)
        return None
    return {
        "ruta": ruta.relative_to(destino).as_posix(),
        "sha256": sha256(datos),
        "bytes": len(datos),
    }


_MSG_EN_CORREO_TXT = re.compile(r"Correo completo \(\.msg\):\s*(\S.*)")


def copiar_mensaje_a_carpetas(
    destino: Path, ruta_msg: Path, rutas_archivos: list[str], simular: bool = False
) -> tuple[int, int]:
    """Copia el `.msg` a la carpeta de cada factura que trajo el correo: `(copiados, faltantes)`.

    La carpeta de la factura (`AAAA/MES/RUC/SERIE-NÚMERO`) sale de las rutas de sus archivos; las
    de auditoría (`_sin_clasificar`, `_otras_empresas`) no son compras y se omiten. El original
    sigue en `_correos/` (es la prueba) y no se sube al portal: el importador solo toma XML y PDF.
    Idempotente.
    """
    carpetas = {
        PurePosixPath(r).parent.as_posix()
        for r in rutas_archivos
        if PurePosixPath(r).parts and PurePosixPath(r).parts[0][:1].isdigit()
    }
    copiados = faltantes = 0
    for carpeta in sorted(carpetas):
        sitio = destino / carpeta
        if not sitio.is_dir() or not ruta_msg.is_file():
            faltantes += 1
            continue
        copia = sitio / f"correo_{ruta_msg.name}"
        if copia.is_file() and copia.stat().st_size == ruta_msg.stat().st_size:
            continue
        if not simular:
            shutil.copy2(ruta_msg, copia)
        copiados += 1
    return copiados, faltantes


def mensajes_a_carpetas(args: argparse.Namespace) -> int:
    """Trae cada `.msg` de `_correos/` a las carpetas de sus facturas (sin tocar Outlook)."""
    destino = Path(args.destino).expanduser().resolve()
    correos = copiados = faltantes = sin_msg = 0
    for ruta_json in sorted((destino / "_correos").rglob("*.json")):
        try:
            datos = json.loads(ruta_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        ruta_msg = ruta_json.with_suffix(".msg")
        if not ruta_msg.is_file():
            sin_msg += 1
            continue
        correos += 1
        c, f = copiar_mensaje_a_carpetas(
            destino, ruta_msg, [a["ruta"] for a in datos.get("archivos", [])], args.simular
        )
        copiados += c
        faltantes += f
    # Carpetas que el portal armó al unir un PDF suelto o restaurar de R2: no figuran en el JSON del
    # correo, pero su `correo.txt` indica de cuál `.msg` salieron.
    for texto in sorted(destino.glob("[0-9][0-9][0-9][0-9]/*/*/*/correo.txt")):
        coincidencia = _MSG_EN_CORREO_TXT.search(
            texto.read_text(encoding="utf-8", errors="replace")
        )
        ruta_msg = destino / coincidencia.group(1).strip() if coincidencia else None
        if ruta_msg is not None and ruta_msg.is_file():
            carpeta = texto.parent.relative_to(destino).as_posix()
            c, f = copiar_mensaje_a_carpetas(
                destino, ruta_msg, [f"{carpeta}/correo.txt"], args.simular
            )
            copiados += c
            faltantes += f
    print(
        f"Correos con .msg: {correos} | copias {'por hacer' if args.simular else 'hechas'}: "
        f"{copiados} | carpetas inexistentes: {faltantes} | JSON sin .msg: {sin_msg}"
    )
    return 0


def _registrar(
    destino: Path,
    ruta_registro: Path,
    ruta_indice: Path,
    indice: dict[tuple[str, int], list[dict[str, Any]]],
    correo: Any,
    carpeta: Any,
    recibido: datetime,
    identificador: str,
    plan: PlanCorreo,
    info: MetaCorreo,
) -> None:
    """Deja constancia del correo: de dónde salió cada archivo y qué comprobantes aportó."""
    meta = (
        destino
        / "_correos"
        / periodo_carpeta(recibido)
        / f"{recibido:%Y%m%d-%H%M%S}_{identificador[:6]}.json"
    )
    meta.parent.mkdir(parents=True, exist_ok=True)
    mensaje = _guardar_mensaje_original(correo, meta.with_suffix(".msg"), destino)
    if mensaje is not None:
        copiar_mensaje_a_carpetas(
            destino, meta.with_suffix(".msg"), [c.ruta for c in plan.colocaciones]
        )
    meta.write_text(
        json.dumps(
            {
                "id": identificador,
                "recibido": recibido.isoformat(),
                "carpeta_correo": str(carpeta.Name),
                "asunto": info.asunto,
                "remitente": info.remitente,
                "para": info.para,
                "mensaje_original": mensaje,
                "archivos": [
                    {
                        "ruta": c.ruta,
                        "origen": c.origen,
                        "sha256": sha256(c.datos),
                        "bytes": len(c.datos),
                    }
                    for c in plan.colocaciones
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    with ruta_indice.open("a", encoding="utf-8") as archivo:
        for c in plan.comprobantes:
            fila = {
                "serie": c.serie,
                "correlativo": c.correlativo,
                "ruc_emisor": c.ruc_emisor,
                "carpeta": c.carpeta,
                "empresa": c.empresa,
            }
            if fila not in indice.get((c.serie, c.correlativo), []):
                indice.setdefault((c.serie, c.correlativo), []).append(fila)
                archivo.write(json.dumps(fila, ensure_ascii=False) + "\n")
    with ruta_registro.open("a", encoding="utf-8") as archivo:
        archivo.write(
            json.dumps(
                {
                    "id": identificador,
                    "recibido": recibido.isoformat(),
                    "carpeta": str(carpeta.Name),
                },
                ensure_ascii=False,
            )
            + "\n"
        )


def respaldar_mensajes(args: argparse.Namespace) -> int:
    """Guarda el `.msg` original de los correos que ya se extrajeron y aún no lo tienen.

    Los correos procesados antes de existir el respaldo solo dejaron un JSON (asunto, remitente,
    huellas de los adjuntos). Aquí se recorre el buzón (solo lectura) y, para cada correo con JSON
    en `_correos/` sin `mensaje_original`, se guarda su `.msg` y se anota su ruta y SHA-256. Es la
    prueba de que el correo existió tal como llegó, aunque luego se borre del buzón.
    """
    try:
        import win32com.client  # type: ignore[import-not-found]
    except ImportError:
        print("Falta pywin32: ejecute con `uv run --with pywin32 python -m ...`")
        return 2

    destino = Path(args.destino).expanduser().resolve()
    nombres = {n.strip().lower() for n in args.carpetas.split(",") if n.strip()}
    espacio = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
    buzones = [b for b in espacio.Folders if args.buzon.lower() in str(b.Name).lower()]
    if len(buzones) != 1:
        print(f"Se esperaba 1 buzón con «{args.buzon}» y hay {len(buzones)}.")
        return 2

    pendientes: dict[str, tuple[Path, dict[str, Any]]] = {}
    for ruta in sorted((destino / "_correos").rglob("*.json")):
        try:
            datos = json.loads(ruta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not datos.get("mensaje_original"):
            pendientes[str(datos["id"])] = (ruta, datos)
    print(f"Correos sin respaldo .msg: {len(pendientes)}")
    guardados = errores = 0
    for carpeta in _carpetas(buzones[0], nombres):
        items = carpeta.Items
        for posicion in range(1, items.Count + 1):
            if not pendientes:
                break
            try:
                correo = items.Item(posicion)
                if correo.Class != OL_MAIL:
                    continue
                identificador = id_correo(str(correo.EntryID))
                if identificador not in pendientes:
                    continue
                ruta_json, datos = pendientes[identificador]
                mensaje = _guardar_mensaje_original(correo, ruta_json.with_suffix(".msg"), destino)
                if mensaje is None:
                    errores += 1
                    continue
                datos["mensaje_original"] = mensaje
                ruta_json.write_text(
                    json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                del pendientes[identificador]
                guardados += 1
            except Exception as exc:
                errores += 1
                print(f"    correo {posicion}: {type(exc).__name__}", file=sys.stderr)
    print(
        f"Respaldados: {guardados} | con error: {errores} | "
        f"no encontrados en el buzón (borrados o movidos): {len(pendientes)}"
    )
    return 1 if errores else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extrae facturas de Outlook (solo lectura)")
    parser.add_argument("--buzon", default="", help="Parte del nombre del buzón en Outlook")
    parser.add_argument("--destino", default="~/Downloads/FacturasProveedores")
    parser.add_argument(
        "--ruc", default=RUC_EMPRESA_POR_DEFECTO, help="RUC de la empresa compradora"
    )
    parser.add_argument(
        "--carpetas", default=CARPETAS_POR_DEFECTO, help="Nombres separados por coma"
    )
    parser.add_argument("--desde", help="Fecha de recepción mínima (AAAA-MM-DD)")
    parser.add_argument("--hasta", help="Fecha de recepción máxima (AAAA-MM-DD)")
    parser.add_argument("--limite", type=int, default=0, help="Máximo de correos con adjuntos")
    parser.add_argument("--simular", action="store_true", help="No escribe nada: solo cuenta")
    parser.add_argument(
        "--sin-paquetes", action="store_true", help="No crear los ZIP de importación"
    )
    parser.add_argument(
        "--respaldar-mensajes",
        action="store_true",
        help="Solo guarda el .msg original de los correos ya extraídos que no lo tienen",
    )
    parser.add_argument(
        "--mensajes-a-carpetas",
        action="store_true",
        help="Solo copia cada .msg de _correos a la carpeta de sus facturas (--simular: cuenta)",
    )
    args = parser.parse_args(argv)
    if args.mensajes_a_carpetas:
        return mensajes_a_carpetas(args)
    if not args.buzon:
        parser.error("--buzon es obligatorio (salvo con --mensajes-a-carpetas)")
    return respaldar_mensajes(args) if args.respaldar_mensajes else ejecutar(args)


if __name__ == "__main__":
    sys.exit(main())
