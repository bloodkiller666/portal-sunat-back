"""Parseo del CDR (Constancia de Recepción).

SUNAT devuelve el CDR como un ZIP en base64 cuyo único XML es un
`ApplicationResponse` UBL 2.1 con:

* `cac:DocumentResponse/cac:Response/cbc:ResponseCode` — 0 = aceptado.
* `cac:DocumentResponse/cac:Response/cbc:Description` — detalle del resultado.
* `cbc:Note` — observaciones, cuando el comprobante fue aceptado con advertencias.

El parseo busca por nombre local del elemento para tolerar prefijos distintos.
"""

from __future__ import annotations

import base64
import binascii
import io
import zipfile
from datetime import datetime

from lxml import etree

from app.core.errors import CdrParseError
from app.core.logging import get_logger
from app.sunat.models import CdrInfo

logger = get_logger(__name__)

_FIRMA_ZIP = b"PK"
_FORMATOS_FECHA = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


def _localname(element: etree._Element) -> str:
    try:
        return etree.QName(element).localname
    except (ValueError, TypeError):
        return ""


def _parse_fecha(datos: dict[str, str]) -> datetime | None:
    fecha = datos.get("ResponseDate") or datos.get("IssueDate")
    hora = datos.get("ResponseTime") or datos.get("IssueTime")
    if not fecha:
        return None
    candidatos = [f"{fecha}T{hora}"] if hora else []
    candidatos.append(fecha)
    for candidato in candidatos:
        for formato in _FORMATOS_FECHA:
            try:
                return datetime.strptime(candidato, formato)
            except ValueError:
                continue
    return None


def parse_cdr_xml(raw: bytes | str) -> CdrInfo:
    """Interpreta el XML del CDR."""
    contenido = raw.encode("utf-8") if isinstance(raw, str) else raw
    try:
        root = etree.fromstring(contenido)
    except etree.XMLSyntaxError as exc:
        raise CdrParseError("El XML del CDR está mal formado") from exc

    codigo: str | None = None
    descripcion: str | None = None
    notas: list[str] = []
    datos_fecha: dict[str, str] = {}

    for elemento in root.iter():
        if not isinstance(elemento.tag, str):
            continue
        nombre = _localname(elemento)
        texto = (elemento.text or "").strip()

        if nombre == "ResponseCode" and codigo is None and texto:
            codigo = texto
        elif nombre == "Description" and descripcion is None and texto:
            descripcion = texto
        elif nombre == "Note" and texto and texto not in notas:
            notas.append(texto)
        elif nombre in {"ResponseDate", "ResponseTime", "IssueDate", "IssueTime"} and texto:
            datos_fecha.setdefault(nombre, texto)

    if codigo is None:
        raise CdrParseError("El CDR no contiene el código de respuesta (ResponseCode)")

    identificador = next(
        (
            (elemento.text or "").strip()
            for elemento in root
            if _localname(elemento) == "ID" and (elemento.text or "").strip()
        ),
        None,
    )

    return CdrInfo(
        codigo=codigo,
        descripcion=descripcion or "",
        notas=notas,
        archivo=None,
        identificador=identificador,
        fecha_respuesta=_parse_fecha(datos_fecha),
        xml=contenido.decode("utf-8", errors="replace"),
    )


def _as_zip_bytes(content: str | bytes | bytearray) -> bytes:
    """Normaliza el contenido: zeep ya decodifica `base64Binary` a bytes."""
    if isinstance(content, (bytes, bytearray)):
        data = bytes(content)
        if data.startswith(_FIRMA_ZIP):
            return data
        try:
            return base64.b64decode(data, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise CdrParseError("El contenido del CDR no es un ZIP ni base64 válido") from exc

    texto = content.strip()
    if not texto:
        raise CdrParseError("SUNAT devolvió un CDR vacío")
    try:
        return base64.b64decode(texto, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise CdrParseError("El CDR no está codificado en base64") from exc


def parse_cdr_zip(content: str | bytes | bytearray) -> CdrInfo:
    """Descomprime el ZIP del CDR y devuelve su contenido interpretado."""
    data = _as_zip_bytes(content)
    if not data.startswith(_FIRMA_ZIP):
        raise CdrParseError("El contenido del CDR no es un archivo ZIP válido")

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archivo:
            nombres = [n for n in archivo.namelist() if n.lower().endswith(".xml")]
            if not nombres:
                raise CdrParseError("El ZIP del CDR no contiene ningún archivo XML")
            xml = archivo.read(nombres[0])
    except zipfile.BadZipFile as exc:
        raise CdrParseError("No se pudo abrir el ZIP del CDR") from exc

    info = parse_cdr_xml(xml)
    return info.model_copy(update={"archivo": nombres[0]})
