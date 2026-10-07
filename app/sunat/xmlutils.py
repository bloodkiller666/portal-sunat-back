"""Utilidades XML: saneamiento de tramas antes de guardarlas o loguearlas.

El header `wsse:Security` viaja con el usuario y la clave SOL en texto. Antes de
registrar cualquier trama se elimina por completo (regla 2).
"""

from __future__ import annotations

from typing import Any

from lxml import etree

from app.core.logging import get_logger

logger = get_logger(__name__)

MAX_XML_LEN = 100_000
TRUNCATED_MARK = "\n<!-- trama truncada -->"

_ELEMENTOS_SENSIBLES = frozenset({"Security", "BinarySecurityToken", "UsernameToken"})


def _localname(tag: Any) -> str:
    try:
        return etree.QName(tag).localname
    except (ValueError, TypeError):  # comentarios, procesamiento de instrucciones
        return ""


def strip_sensitive_elements(root: Any) -> int:
    """Elimina los elementos de seguridad de un árbol lxml. Devuelve cuántos quitó."""
    removidos = 0
    for elemento in list(root.iter()):
        if not isinstance(elemento.tag, str):
            continue
        if _localname(elemento.tag) in _ELEMENTOS_SENSIBLES:
            padre = elemento.getparent()
            if padre is not None:
                padre.remove(elemento)
                removidos += 1
    return removidos


def sanitize_xml(value: Any) -> str | None:
    """Convierte a texto y quita credenciales. Acepta `Element`, bytes o `str`.

    Nunca falla: si el XML es inválido se devuelve un marcador, porque guardar
    basura es preferible a registrar secretos.
    """
    if value is None:
        return None

    if isinstance(value, (str, bytes, bytearray)):
        texto = (
            value.decode("utf-8", errors="replace")
            if isinstance(value, (bytes, bytearray))
            else value
        )
    else:
        texto = None

    try:
        if texto is None:
            copia = etree.fromstring(etree.tostring(value))
        else:
            copia = etree.fromstring(texto.encode("utf-8"))
    except etree.XMLSyntaxError:
        # No es XML parseable: no se pueden quitar elementos, así que se aplica la
        # redacción de texto antes de recortar.
        from app.core.redaction import redact

        return redact(texto or "")[:MAX_XML_LEN]
    except Exception:
        logger.warning("No se pudo sanear una trama XML; se omite su contenido")
        return None

    strip_sensitive_elements(copia)
    try:
        texto = etree.tostring(copia, encoding="unicode")
    except Exception:
        logger.warning("No se pudo serializar una trama XML saneada; se omite")
        return None

    if len(texto) > MAX_XML_LEN:
        texto = texto[:MAX_XML_LEN] + TRUNCATED_MARK
    return texto
