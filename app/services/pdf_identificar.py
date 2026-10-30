"""Identifica a qué factura pertenece un PDF leyendo su texto (sin OCR).

Un PDF suelto (sin su XML) no dice por sí solo de qué compra es, pero casi siempre imprime el RUC
del proveedor y la serie-número. Aquí solo se EXTRAEN los candidatos de las primeras páginas; quien
decide es el servicio, que acepta una unión únicamente si el RUC y la serie-número coinciden EXACTO
con una compra ya registrada. Un PDF escaneado (sin texto) no se puede identificar.

El PDF viene de un correo externo: se limita su tamaño y las páginas leídas, y cualquier fallo del
lector se trata como «no identificado» (nunca detiene la importación).
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

from pypdf import PdfReader

MAX_BYTES_PDF = 25 * 1024 * 1024
MAX_PAGINAS = 2
MIN_TEXTO = 30  # menos que esto es un escaneo o un PDF vacío

_RUC = re.compile(r"(?<!\d)(?:10|15|17|20)\d{9}(?!\d)")
# El guion puede ser el corto o el largo (U+2013): los PDF de los proveedores usan ambos.
_GUIONES = "-" + chr(0x2013)
_SERIE_NUMERO = re.compile(
    r"(?<![A-Z0-9])([A-Z][A-Z0-9]{3})\s*[" + _GUIONES + r"]\s*0*(\d{1,8})(?!\d)", re.IGNORECASE
)


# Orden de compra de TDM (SAP): 10 dígitos que empiezan en 45 (4500054453).
_ORDEN_COMPRA = re.compile(r"(?<!\d)45\d{8}(?!\d)")
# Serie de una guía de remisión o de una factura: 1-2 letras y 2-3 dígitos (EG07, T001, F015).
_SERIE_DE_DOCUMENTO = re.compile(r"^[A-Z]{1,2}\d{2,3}$")


@dataclass(frozen=True)
class Referencias:
    """Las órdenes de compra y las guías de remisión que cita (o lleva) un documento.

    Una factura cita su OC y su guía (`OC 4500054453 / GRR EG07-1145`); la OC en PDF lleva su
    número y la guía en PDF lleva el suyo y la OC en las observaciones. Con eso se sabe de qué
    factura es cada PDF aunque ninguno nombre la serie-número de la factura.
    """

    ordenes: frozenset[str] = frozenset()
    guias: frozenset[tuple[str, int]] = frozenset()

    def vacia(self) -> bool:
        return not self.ordenes and not self.guias

    def coincide(self, otra: Referencias) -> bool:
        return bool(self.ordenes & otra.ordenes or self.guias & otra.guias)


def referencias_de_texto(texto: str, propia: tuple[str, int] | None = None) -> Referencias:
    """OC y guías que figuran en un texto. `propia` (la serie-número de la factura) no cuenta."""
    ordenes = frozenset(_ORDEN_COMPRA.findall(texto))
    guias = frozenset(
        (m.group(1).upper(), int(m.group(2)))
        for m in _SERIE_NUMERO.finditer(texto)
        if _SERIE_DE_DOCUMENTO.match(m.group(1).upper())
        and (m.group(1).upper(), int(m.group(2))) != propia
    )
    return Referencias(ordenes, guias)


def referencias_de_pdf(contenido: bytes) -> Referencias:
    """OC y guías de un PDF (vacías si es un escaneo sin texto)."""
    return referencias_de_texto(texto_del_pdf(contenido))


def referencias_de_factura(xml: bytes, pdfs: list[bytes], propia: tuple[str, int]) -> Referencias:
    """OC y guías de una factura: las del XML (sus referencias) y las de su propio PDF."""
    texto = xml.decode("utf-8", errors="replace") + " " + " ".join(texto_del_pdf(p) for p in pdfs)
    return referencias_de_texto(texto, propia)


def texto_del_pdf(contenido: bytes) -> str:
    """Texto de las primeras páginas, o cadena vacía si no se puede leer."""
    if not contenido or len(contenido) > MAX_BYTES_PDF:
        return ""
    try:
        lector = PdfReader(io.BytesIO(contenido), strict=False)
        return " ".join((pagina.extract_text() or "") for pagina in lector.pages[:MAX_PAGINAS])
    except Exception:
        return ""


def candidatos_de_pdf(contenido: bytes) -> tuple[set[str], set[tuple[str, int]]]:
    """`(RUC, (serie, correlativo))` que figuran en el texto del PDF."""
    texto = texto_del_pdf(contenido)
    if len(texto.strip()) < MIN_TEXTO:
        return set(), set()
    rucs = set(_RUC.findall(texto))
    series = {(m.group(1).upper(), int(m.group(2))) for m in _SERIE_NUMERO.finditer(texto)}
    return rucs, series
