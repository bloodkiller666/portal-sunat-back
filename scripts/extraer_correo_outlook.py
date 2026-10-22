"""Extrae las facturas de proveedores de un buzón de Outlook clásico y las ordena por carpetas.

SOLO LECTURA sobre el buzón: no borra, mueve ni marca ningún correo. Cada comprobante queda en

    {destino}/AAAA/MM/RUC/SERIE-NÚMERO/        XML, PDF y CDR de ESA factura
        (AAAA/MM = fecha de emisión; RUC = el del proveedor; el nombre es serie-número de 8 dígitos)

y lo que no se puede ubicar se aparta para revisarlo a mano:

    {destino}/_otras_empresas/{RUC adquirente}/AAAA/MM/RUC/SERIE-NÚMERO/    facturas a otro RUC
    {destino}/_sin_clasificar/AAAA-MM/{correo}/        PDF sin XML, CDR sueltos…
    {destino}/_correos/AAAA-MM/{correo}.json        de qué correo salió cada archivo (auditoría)
    {destino}/_paquetes_importar/{fecha}/lote-NNN.zip    ZIP para el botón «Importar» (Compras)
    {destino}/registro_correos.jsonl, indice_comprobantes.jsonl, informe.json

La carpeta de cada compra sale del XML (proveedor, serie y número, fecha de emisión), no del
correo. Un PDF sin XML no dice a qué documento pertenece, por eso va a `_sin_clasificar`. Las
reglas para unir PDF y CDR a su factura son las mismas del importador del backend
(`app.services.proveedores_paquete`). Al final de cada ejecución se intenta unir los CDR (y los
PDF con serie-número en el nombre) que llegaron en otro correo.

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
from app.services.proveedores_paquete import (
    Asignacion,
    Entrada,
    agrupar,
    raiz_xml,
    referencia_cdr,
    serie_numero_de,
)
from app.sunat.ubl import parse_comprobante_ubl

OL_MAIL = 43  # OlObjectClass.olMail
OL_BY_VALUE = 1  # OlAttachmentType.olByValue: archivo real
GUARDAR = {"xml", "pdf"}  # se guardan tal cual
ABRIR = {"zip"}  # se abren; el original se conserva
APARTE = {"rar", "7z", ""}  # se apartan para revisión manual (sin extensión incluida)
DE_FIRMA = {"png", "jpg", "jpeg", "gif", "bmp", "ico", "emf", "wmf", "svg"}

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
    return destino / f"{recibido:%Y-%m}" / f"{recibido:%Y%m%d-%H%M%S}_{identificador[:6]}"


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
    """XML y PDF de un ZIP, con topes de tamaño. Devuelve `(archivos, motivos_de_omision)`.

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
        if ext not in GUARDAR:
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


def carpeta_de_comprobante(
    ruc_emisor: str,
    serie: str,
    correlativo: int,
    emision: date | None,
    adquirente: str | None,
    ruc_empresa: str,
) -> str:
    """`AAAA/MM/RUC/SERIE-NÚMERO` (o bajo `_otras_empresas/{RUC}` si compra otro RUC)."""
    anio, mes = (f"{emision.year:04d}", f"{emision.month:02d}") if emision else ("sin-fecha",) * 2
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
) -> PlanCorreo:
    """Decide dónde va cada archivo de UN correo. `archivos` = `(nombre, datos, origen)`."""
    plan = PlanCorreo()
    origenes: dict[str, str] = {}
    entradas: list[Entrada] = []
    usados: set[str] = set()
    for nombre, datos, origen in archivos:
        if origen == "zip_original":
            # El ZIP se conserva aparte: su contenido ya se reparte con los demás adjuntos.
            ruta = f"_originales_zip/{recibido:%Y-%m}/{recibido:%Y%m%d-%H%M%S}_{identificador[:6]}"
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
    for asignacion in agrupacion.asignaciones:
        destino = _carpeta_de(asignacion, ruc_empresa, plan)
        if destino is not None:
            destino_unico = destino if len(agrupacion.asignaciones) == 1 else None
        for archivo in _archivos_de(asignacion):
            ruta = destino if destino is not None else _sin_clasificar(recibido, identificador)
            plan.colocaciones.append(
                Colocacion(f"{ruta}/{archivo.nombre}", archivo.contenido, origenes[archivo.nombre])
            )
            if destino is None:
                plan.sin_clasificar.append((archivo.nombre, "xml_no_valido"))

    for archivo, codigo, _texto in agrupacion.ignorados:
        if codigo == "tipo_no_soportado":
            codigo = "revisar_a_mano"  # RAR, archivos sin extensión…
        # Con una sola factura en el correo, su guía de remisión va con ella.
        ruta = destino_unico if codigo == "guia" and destino_unico else None
        ruta = ruta or _sin_clasificar(recibido, identificador)
        if ruta.startswith("_sin_clasificar"):
            plan.sin_clasificar.append((archivo.nombre, codigo))
        plan.colocaciones.append(
            Colocacion(f"{ruta}/{archivo.nombre}", archivo.contenido, origenes[archivo.nombre])
        )
    return plan


def _sin_clasificar(recibido: datetime, identificador: str) -> str:
    return f"_sin_clasificar/{recibido:%Y-%m}/{recibido:%Y%m%d-%H%M%S}_{identificador[:6]}"


def _archivos_de(asignacion: Asignacion) -> list[Entrada]:
    return [asignacion.xml, *asignacion.pdfs, *([asignacion.cdr] if asignacion.cdr else [])]


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
    el PDF y el CDR deben viajar con su XML.
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
            if tocadas is not None:
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
    """Une lo que llegó en otro correo: CDR por su referencia y PDF por la serie-número del nombre.

    Solo se une cuando el destino es único (una sola compra con esa serie-número) y todavía no
    tiene ese tipo de archivo: ante la duda el archivo se queda en `_sin_clasificar`.
    """
    unidos: Counter[str] = Counter()
    base = destino / "_sin_clasificar"
    if not base.is_dir():
        return unidos
    for archivo in sorted(p for p in base.rglob("*") if p.is_file()):
        ext = extension(archivo.name)
        clave: tuple[str, int] | None = None
        if ext == "xml" and raiz_xml(archivo.read_bytes()) == "ApplicationResponse":
            clave, tipo = referencia_cdr(archivo.read_bytes()), "cdr"
        elif ext == "pdf":
            clave, tipo = serie_numero_de(archivo.name), "pdf"
        else:
            continue
        candidatos = [f for f in indice.get(clave, []) if f["empresa"]] if clave else []
        if len(candidatos) != 1:
            continue
        carpeta = destino / candidatos[0]["carpeta"]
        tiene = any(
            f.suffix.lower() == (".pdf" if tipo == "pdf" else ".xml")
            and (tipo == "pdf" or raiz_xml(f.read_bytes()) == "ApplicationResponse")
            for f in carpeta.glob("*")
        )
        if tiene:
            continue
        carpeta.mkdir(parents=True, exist_ok=True)
        shutil.move(str(archivo), str(nombre_libre(carpeta, archivo.name)))
        unidos[tipo] += 1
        if tocadas is not None:
            tocadas.add(candidatos[0]["carpeta"])
    return unidos


def crear_paquetes(destino: Path, simular: bool) -> list[Path]:
    """ZIP con las carpetas de compras de la empresa, listos para el botón «Importar»."""
    carpetas: list[tuple[str, list[tuple[str, int]]]] = []
    for ruta in sorted(destino.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/*/*")):
        if ruta.is_dir():
            archivos = [(f.name, f.stat().st_size) for f in ruta.iterdir() if f.is_file()]
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
                    if archivo.is_file():
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
        if ext not in GUARDAR | ABRIR | APARTE:
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
        if ext in GUARDAR:
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
                )
                informe.con_facturas += 1
                for c in plan.comprobantes:  # sin repetir la factura que llegó en varios correos
                    (vistos_empresa if c.empresa else vistos_otras).add(c.carpeta)
                informe.sin_clasificar.update(motivo for _, motivo in plan.sin_clasificar)
                if args.simular:
                    continue
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
) -> None:
    """Deja constancia del correo: de dónde salió cada archivo y qué comprobantes aportó."""
    meta = (
        destino
        / "_correos"
        / f"{recibido:%Y-%m}"
        / f"{recibido:%Y%m%d-%H%M%S}_{identificador[:6]}.json"
    )
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(
        json.dumps(
            {
                "id": identificador,
                "recibido": recibido.isoformat(),
                "carpeta_correo": str(carpeta.Name),
                "asunto": str(getattr(correo, "Subject", "") or ""),
                "remitente": str(getattr(correo, "SenderName", "") or ""),
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extrae facturas de Outlook (solo lectura)")
    parser.add_argument("--buzon", required=True, help="Parte del nombre del buzón en Outlook")
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
    return ejecutar(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
