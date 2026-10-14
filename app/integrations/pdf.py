"""Representación impresa del comprobante electrónico en PDF.

Genera un PDF con la estructura de la representación impresa que exige SUNAT
para un CPE (cabecera del emisor, cuadro del tipo de documento, adquirente,
detalle de ítems y totales), a partir de los datos ya registrados en la base.

Usa `fpdf2` (Python puro, sin dependencias nativas) y las fuentes base del PDF,
de modo que funciona igual en Windows y en el contenedor de producción.
No toca SUNAT ni valida la firma: solo imprime lo que la base ya registró.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from fpdf import FPDF
from fpdf.fonts import FontFace

from app.core.concurrency import run_blocking
from app.db.models import Comprobante, ComprobanteItem

# Etiquetas oficiales por código de tipo de comprobante (catálogo 01).
_ETIQUETAS_TIPO = {
    "01": "FACTURA ELECTRÓNICA",
    "03": "BOLETA DE VENTA ELECTRÓNICA",
    "07": "NOTA DE CRÉDITO ELECTRÓNICA",
    "08": "NOTA DE DÉBITO ELECTRÓNICA",
}

_FUENTE = "Helvetica"
_COLOR_GRIS_CABECERA = (235, 235, 235)
_ANCHO_UTIL = 186.0  # A4 (210 mm) con márgenes de 12 mm
_SIN_DATO = "-"

# Sustituciones típicas que caen fuera de latin-1 antes del recorte final.
_REEMPLAZOS = {
    "\u2013": "-",  # en dash
    "\u2014": "-",  # em dash
    "\u2018": "'",
    "\u2019": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u2026": "...",
}


@runtime_checkable
class PdfRenderer(Protocol):
    """Contrato de generación de PDF."""

    async def render(
        self, comprobante: Comprobante, items: Sequence[ComprobanteItem] = ()
    ) -> bytes: ...


class SunatPdfRenderer:
    """Implementación de `PdfRenderer`: comprobante (+ detalle) → PDF."""

    def __init__(self, *, logo_url: str | None = None, plantilla: str | None = None) -> None:
        # Reservados para personalización futura (logo de la empresa, plantilla
        # alternativa). Hoy la maqueta es fija.
        self._logo_url = logo_url
        self._plantilla = plantilla

    async def render(
        self, comprobante: Comprobante, items: Sequence[ComprobanteItem] = ()
    ) -> bytes:
        """Genera el PDF del comprobante (CPU-bound: corre en un hilo)."""
        return await run_blocking(_render_pdf, comprobante, list(items))


def _render_pdf(comprobante: Comprobante, items: list[ComprobanteItem]) -> bytes:
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_margins(left=12, top=12, right=12)
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()

    _cabecera(pdf, comprobante)
    _adquirente(pdf, comprobante)
    _detalle(pdf, items)
    _totales(pdf, comprobante, items)
    _pie(pdf, comprobante)

    return bytes(pdf.output())


def _cabecera(pdf: FPDF, comprobante: Comprobante) -> None:
    """Razón social y RUC del emisor + cuadro del tipo de documento y número."""
    pdf.set_font(_FUENTE, "B", 13)
    pdf.set_xy(12, 12)
    pdf.cell(110, 7, _texto_pdf(comprobante.razon_social_emisor or "(sin razón social)"))
    pdf.set_font(_FUENTE, "", 10)
    pdf.set_xy(12, 19)
    pdf.cell(110, 5, f"RUC: {comprobante.ruc_emisor}")

    etiqueta = _ETIQUETAS_TIPO.get(comprobante.tipo_documento, "COMPROBANTE ELECTRÓNICO")
    caja_x, caja_y, caja_w, caja_h = 132.0, 12.0, 66.0, 18.0
    pdf.set_font(_FUENTE, "B", 9)
    pdf.set_xy(caja_x, caja_y + 3)
    pdf.cell(caja_w, 5, _texto_pdf(etiqueta), align="C")
    pdf.set_font(_FUENTE, "B", 13)
    pdf.set_xy(caja_x, caja_y + 9)
    pdf.cell(caja_w, 6, f"{comprobante.serie}-{comprobante.numero}", align="C")
    pdf.rect(caja_x, caja_y, caja_w, caja_h)

    pdf.set_draw_color(120, 120, 120)
    pdf.line(12, 34, 198, 34)

    # Ficha de metadatos: emisión, moneda, estado ante SUNAT y datos de gestión.
    col_izq: list[tuple[str, str]] = [
        (
            "Fecha de emisión",
            comprobante.fecha_emision.strftime("%d/%m/%Y")
            if comprobante.fecha_emision
            else _SIN_DATO,
        ),
        ("Moneda", comprobante.moneda or "PEN"),
        ("Estado SUNAT", comprobante.estado.etiqueta),
    ]
    col_der: list[tuple[str, str]] = []
    if comprobante.n_sap:
        col_der.append(("N° SAP", comprobante.n_sap))
    if comprobante.orden_compra:
        col_der.append(("Orden de compra", comprobante.orden_compra))
    if comprobante.cdr_descripcion:
        col_der.append(("SUNAT", _acotar(comprobante.cdr_descripcion, 60)))

    y = 38.0
    for titulo, valor in col_izq:
        pdf.set_font(_FUENTE, "", 9)
        pdf.set_xy(12, y)
        pdf.cell(32, 5, _texto_pdf(f"{titulo}:"))
        pdf.set_font(_FUENTE, "B", 9)
        pdf.cell(80, 5, _texto_pdf(valor))
        y += 5
    y = 38.0
    for titulo, valor in col_der:
        pdf.set_font(_FUENTE, "", 9)
        pdf.set_xy(112, y)
        pdf.cell(28, 5, _texto_pdf(f"{titulo}:"))
        pdf.set_font(_FUENTE, "", 8)
        pdf.cell(58, 5, _texto_pdf(valor))
        y += 5


def _adquirente(pdf: FPDF, comprobante: Comprobante) -> None:
    """Bloque del adquirente (cliente en ventas, la propia empresa en compras)."""
    y = max(pdf.get_y(), 62.0)
    pdf.set_draw_color(120, 120, 120)

    pdf.set_font(_FUENTE, "B", 9)
    pdf.set_xy(12, y)
    pdf.cell(60, 5, "Adquirente")

    documento = comprobante.receptor_doc or comprobante.ruc_receptor or _SIN_DATO
    tipo_doc = (
        ("RUC" if len(documento) == 11 else "DNI") if documento.isdigit() else "Doc."
    )

    pdf.set_font(_FUENTE, "", 9)
    pdf.set_xy(12, y + 6)
    pdf.cell(26, 5, _texto_pdf(f"{tipo_doc}:"))
    pdf.cell(80, 5, _texto_pdf(documento))

    pdf.set_xy(12, y + 11)
    pdf.cell(26, 5, "Razón social:")
    pdf.multi_cell(140, 5, _texto_pdf(comprobante.razon_social_receptor or _SIN_DATO))

    borde = max(pdf.get_y() + 2, y + 18)
    pdf.line(12, borde, 198, borde)
    pdf.set_y(borde + 2)


def _detalle(pdf: FPDF, items: list[ComprobanteItem]) -> None:
    """Tabla de líneas del comprobante."""
    pdf.set_font(_FUENTE, "B", 9)
    pdf.set_x(12)
    pdf.cell(60, 6, "Detalle del comprobante")
    pdf.ln(7)

    if not items:
        pdf.set_font(_FUENTE, "I", 8)
        pdf.cell(0, 5, "(El comprobante no tiene líneas de detalle registradas)")
        pdf.ln(7)
        return

    titulos = ("N°", "Cantidad", "Und.", "Descripción", "P. Unit. (incl. IGV)", "Valor venta")
    estilo_cabecera = FontFace(emphasis="BOLD", fill_color=_COLOR_GRIS_CABECERA)
    with pdf.table(
        headings_style=estilo_cabecera,
        col_widths=(10, 18, 14, 88, 28, 28),
        text_align=("CENTER", "CENTER", "CENTER", "LEFT", "RIGHT", "RIGHT"),
        line_height=4.6,
        padding=1.2,
    ) as table:
        cabecera = table.row()
        for titulo in titulos:
            cabecera.cell(titulo)
        for item in items:
            fila = table.row()
            fila.cell(str(item.item_nro))
            fila.cell(_numero(item.cantidad))
            fila.cell(_texto_pdf(item.unidad_medida) or _SIN_DATO)
            fila.cell(_texto_pdf(item.descripcion))
            if item.precio_unitario is not None:
                fila.cell(_dinero(item.precio_unitario))
            else:
                fila.cell(_SIN_DATO)
            if item.total is not None:
                fila.cell(_dinero(item.total))
            else:
                fila.cell(_SIN_DATO)
    pdf.ln(2)


def _totales(pdf: FPDF, comprobante: Comprobante, items: list[ComprobanteItem]) -> None:
    """Resumen de importes, a la derecha: base, IGV y total a pagar."""
    filas: list[tuple[str, str]] = []
    if items:
        base = sum((item.total or Decimal("0") for item in items), Decimal("0"))
        igv = sum((item.igv or Decimal("0") for item in items), Decimal("0"))
        filas = [("Base imponible", _dinero(base)), ("IGV", _dinero(igv))]

    total = comprobante.monto_total
    if total is None and items:
        total = base + igv

    x, ancho = 128.0, 70.0
    y = pdf.get_y() + 2
    pdf.set_font(_FUENTE, "", 9)
    for titulo, valor in filas:
        pdf.set_xy(x, y)
        pdf.cell(40, 5, titulo, align="R")
        pdf.cell(ancho - 40, 5, valor, align="R")
        y += 5

    pdf.set_font(_FUENTE, "B", 11)
    pdf.set_xy(x, y + 2)
    pdf.cell(40, 7, "TOTAL", align="R")
    pdf.cell(
        ancho - 40,
        7,
        _dinero(total) if total is not None else _SIN_DATO,
        align="R",
    )
    if comprobante.moneda:
        pdf.set_font(_FUENTE, "I", 8)
        pdf.set_xy(x, y + 11)
        pdf.cell(ancho, 4, f"Moneda: {comprobante.moneda}", align="R")


def _pie(pdf: FPDF, comprobante: Comprobante) -> None:
    """Leyenda de la representación impresa, separada por una línea."""
    pdf.set_font(_FUENTE, "", 7)
    pdf.set_text_color(90, 90, 90)
    pdf.ln(6)
    pdf.set_draw_color(180, 180, 180)
    pdf.line(12, pdf.get_y(), 198, pdf.get_y())
    pdf.ln(2)
    nombre = comprobante.nombre_archivo or f"{comprobante.serie}-{comprobante.numero}"
    pdf.multi_cell(
        _ANCHO_UTIL,
        4,
        _texto_pdf(
            f"Representación impresa del comprobante electrónico {nombre}. "
            "Documento generado por el portal de facturación "
            f"el {datetime.now(tz=UTC).strftime('%d/%m/%Y %H:%M')} (UTC)."
        ),
    )
    pdf.set_text_color(0, 0, 0)


def _texto_pdf(valor: str | None) -> str:
    """Las fuentes base del PDF son latin-1: se ajustan los glifos ajenos."""
    if not valor:
        return ""
    for origen, destino in _REEMPLAZOS.items():
        valor = valor.replace(origen, destino)
    return valor.encode("latin-1", "replace").decode("latin-1")


def _dinero(valor: Decimal | None) -> str:
    if valor is None:
        return _SIN_DATO
    return f"{valor:,.2f}"


def _numero(valor: Decimal) -> str:
    """Cantidad sin ceros sobrantes: 2.00 → '2', 2.500 → '2.5'."""
    normalizado = f"{valor.normalize():f}"
    if "E" in normalizado.upper():  # normalize puede dar notación científica
        return f"{valor:f}".rstrip("0").rstrip(".")
    return normalizado


def _acotar(texto: str, maximo: int) -> str:
    limpio = " ".join(texto.split())
    if len(limpio) <= maximo:
        return limpio
    return limpio[: maximo - 3].rstrip() + "..."
