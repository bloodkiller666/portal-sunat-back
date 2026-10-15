"""Extractor de los datos de un comprobante desde su XML UBL 2.1.

SUNAT publica los comprobantes con la estructura UBL 2.1 (Invoice, CreditNote,
DebitNote). Aquí solo se leen los campos que el portal necesita para dar de alta
el comprobante; no se valida la firma ni se emite nada.

La lectura es tolerante a prefijos: se navega por *nombre local* de los
elementos, así que sirve tanto para `cbc:`/`cac:` como para cualquier otro
prefijo equivalente.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from lxml import etree

from app.core.errors import InvalidDocumentError

_TIPOS_POR_RAIZ = {
    "Invoice": None,  # se decide por la serie (F=01, B=03)
    "CreditNote": "07",
    "DebitNote": "08",
}

_PREFIJO_TIPO = {"F": "01", "B": "03"}

_NOMBRE_ARCHIVO_RE = re.compile(r"^([A-Z0-9]{1,4})-([0-9]{1,8})$")

# Elemento que contiene las líneas, según la raíz del documento UBL.
_LINEAS_POR_RAIZ = {
    "Invoice": "InvoiceLine",
    "CreditNote": "CreditNoteLine",
    "DebitNote": "DebitNoteLine",
}

# Elemento de cantidad según el tipo de documento (mismo semántica, distinto nombre).
_CANTIDADES = ("InvoicedQuantity", "CreditedQuantity", "DebitedQuantity")

# Tope defensivo: una factura real rara vez pasa de unas decenas de líneas.
_MAX_ITEMS = 1000

# Esquemas de impuestos del catálogo 5 de SUNAT que se desglosan por ítem.
_ESQUEMA_IGV = "1000"
_ESQUEMA_ISC = "2000"


@dataclass(frozen=True, slots=True)
class ItemUbl:
    """Una línea del comprobante (detalle de bienes o servicios)."""

    item_nro: int
    descripcion: str
    cantidad: Decimal
    unidad_medida: str | None = None
    codigo: str | None = None
    valor_unitario: Decimal | None = None  # sin IGV
    precio_unitario: Decimal | None = None  # con IGV, como lo imprime SUNAT
    descuento: Decimal = Decimal("0")
    igv: Decimal = Decimal("0")
    isc: Decimal = Decimal("0")
    tip_afectacion_igv: str | None = None  # catálogo 07 de SUNAT
    total: Decimal | None = None  # valor de venta de la línea


@dataclass(frozen=True, slots=True)
class ComprobanteUbl:
    """Datos mínimos del comprobante extraídos del XML."""

    tipo_documento: str
    serie: str
    correlativo: int
    ruc_emisor: str
    fecha_emision: date | None
    moneda: str
    monto_total: Decimal | None
    razon_social_emisor: str | None = None
    receptor_doc: str | None = None
    ruc_receptor: str | None = None
    razon_social_receptor: str | None = None
    items: tuple[ItemUbl, ...] = ()
    fecha_vencimiento: date | None = None
    forma_pago: str | None = None

    @property
    def nombre_archivo(self) -> str:
        """Nomenclatura SUNAT: `{RUC}-{TIPO}-{SERIE}-{CORRELATIVO:08d}`."""
        return f"{self.ruc_emisor}-{self.tipo_documento}-{self.serie}-{self.correlativo:08d}"


def _localname(elemento: etree._Element) -> str:
    return etree.QName(elemento).localname


def _hijo(elemento: etree._Element | None, *nombres: str) -> etree._Element | None:
    """Primer descendiente directo cuyo nombre local coincida, en orden."""
    if elemento is None:
        return None
    actual = elemento
    for nombre in nombres:
        siguiente = next(
            (hijo for hijo in actual if isinstance(hijo.tag, str) and _localname(hijo) == nombre),
            None,
        )
        if siguiente is None:
            return None
        actual = siguiente
    return actual


def _texto(elemento: etree._Element | None) -> str | None:
    if elemento is None or elemento.text is None:
        return None
    valor = elemento.text.strip()
    return valor or None


def _texto_en(elemento: etree._Element, nombre: str) -> str | None:
    return _texto(_hijo(elemento, nombre))


def parse_comprobante_ubl(xml: bytes, *, ruc_esperado: str | None = None) -> ComprobanteUbl:
    """Extrae los datos de un XML UBL y valida lo indispensable.

    `ruc_esperado` (opcional) rechaza comprobantes que no pertenezcan a la
    empresa indicada.
    """
    try:
        root = etree.fromstring(xml)
    except etree.XMLSyntaxError as exc:
        raise InvalidDocumentError(
            "El archivo no es un XML válido", details={"detalle": str(exc)[:200]}
        ) from exc

    raiz = _localname(root)
    if raiz not in _TIPOS_POR_RAIZ:
        raise InvalidDocumentError(
            "El XML no es un comprobante UBL (se esperaba Invoice, CreditNote o DebitNote)",
            details={"raiz": raiz},
        )

    identificador = _texto_en(root, "ID")
    if not identificador or not _NOMBRE_ARCHIVO_RE.match(identificador.upper()):
        raise InvalidDocumentError(
            "El comprobante no tiene un identificador válido ({SERIE}-{CORRELATIVO})",
            details={"id": identificador},
        )
    serie, correlativo_txt = _NOMBRE_ARCHIVO_RE.match(identificador.upper()).groups()  # type: ignore[union-attr]
    correlativo = int(correlativo_txt)

    tipo_documento = _TIPOS_POR_RAIZ[raiz]
    if tipo_documento is None:
        tipo_documento = _PREFIJO_TIPO.get(serie[0])
    if tipo_documento is None:
        raise InvalidDocumentError(
            "No se pudo determinar el tipo de comprobante a partir de la serie",
            details={"serie": serie},
        )

    ruc_emisor = _ruc_emisor(root)
    if ruc_emisor is None or not re.fullmatch(r"[0-9]{11}", ruc_emisor):
        raise InvalidDocumentError(
            "El XML no tiene un RUC de emisor válido", details={"ruc": ruc_emisor}
        )
    if ruc_esperado is not None and ruc_emisor != ruc_esperado:
        raise InvalidDocumentError(
            "El comprobante pertenece a otro RUC y no puede importarse en esta empresa",
            details={"ruc_emisor": ruc_emisor, "ruc_empresa": ruc_esperado},
        )

    emisor = _hijo(root, "AccountingSupplierParty", "Party")
    receptor = _hijo(root, "AccountingCustomerParty", "Party")
    receptor_doc = _documento_party(receptor)
    fecha_emision = _fecha(root)
    vencimiento = _vencimiento(root)

    return ComprobanteUbl(
        tipo_documento=tipo_documento,
        serie=serie,
        correlativo=correlativo,
        ruc_emisor=ruc_emisor,
        fecha_emision=fecha_emision,
        fecha_vencimiento=vencimiento,
        forma_pago=_forma_pago(root, fecha_emision, vencimiento),
        moneda=(_texto_en(root, "DocumentCurrencyCode") or "PEN").upper()[:3],
        monto_total=_monto_total(root),
        razon_social_emisor=_razon_social(emisor),
        receptor_doc=receptor_doc,
        ruc_receptor=_ruc_de_documento(receptor_doc),
        razon_social_receptor=_razon_social(receptor),
        items=_parse_items(root, raiz),
    )


def _ruc_emisor(root: etree._Element) -> str | None:
    """RUC del emisor: `PartyIdentification` (schemeID=6) o `PartyLegalEntity`."""
    party = _hijo(root, "AccountingSupplierParty", "Party")
    if party is None:
        return None
    for identificacion in party:
        if not isinstance(identificacion.tag, str):
            continue
        if _localname(identificacion) != "PartyIdentification":
            continue
        if identificacion.get("schemeID") in (None, "6"):
            valor = _texto_en(identificacion, "ID")
            if valor:
                return valor
    return _texto(_hijo(party, "PartyLegalEntity", "CompanyID"))


def _documento_party(party: etree._Element | None) -> str | None:
    """Documento del adquirente tal como figura en el XML (RUC, DNI o '-')."""
    if party is None:
        return None
    for identificacion in party:
        if not isinstance(identificacion.tag, str):
            continue
        if _localname(identificacion) != "PartyIdentification":
            continue
        valor = _texto_en(identificacion, "ID")
        if valor:
            return valor
    return _texto(_hijo(party, "PartyLegalEntity", "CompanyID"))


def _ruc_de_documento(documento: str | None) -> str | None:
    """Solo un RUC de 11 dígitos se puede validar contra una empresa del portal."""
    if documento and re.fullmatch(r"[0-9]{11}", documento):
        return documento
    return None


def _razon_social(party: etree._Element | None) -> str | None:
    """Razón social: `PartyLegalEntity/RegistrationName` o `PartyName/Name`."""
    if party is None:
        return None
    return _texto(_hijo(party, "PartyLegalEntity", "RegistrationName")) or _texto(
        _hijo(party, "PartyName", "Name")
    )


def _fecha(root: etree._Element) -> date | None:
    valor = _texto_en(root, "IssueDate")
    if not valor:
        return None
    try:
        return date.fromisoformat(valor)
    except ValueError:
        return None


def _fecha_iso(valor: str | None) -> date | None:
    if not valor:
        return None
    try:
        return date.fromisoformat(valor[:10])
    except ValueError:
        return None


def _hijos(elemento: etree._Element, nombre: str) -> list[etree._Element]:
    return [
        hijo for hijo in elemento if isinstance(hijo.tag, str) and _localname(hijo) == nombre
    ]


def _vencimiento(root: etree._Element) -> date | None:
    """Fecha de vencimiento: `DueDate` de la cabecera o, si no, la última cuota.

    Un crédito en cuotas declara una `PaymentTerms/PaymentDueDate` por cuota; el
    comprobante vence cuando vence la última. También se tolera el formato
    `Payment/PaymentDueDate` de algunos emisores.
    """
    directa = _fecha_iso(_texto_en(root, "DueDate"))
    if directa is not None:
        return directa
    fechas = [
        fecha
        for termino in _hijos(root, "PaymentTerms")
        if (fecha := _fecha_iso(_texto_en(termino, "PaymentDueDate"))) is not None
    ]
    fechas += [
        fecha
        for pago in _hijos(root, "Payment")
        if (fecha := _fecha_iso(_texto_en(pago, "PaymentDueDate"))) is not None
    ]
    return max(fechas) if fechas else None


def _forma_pago(
    root: etree._Element, emision: date | None, vencimiento: date | None
) -> str | None:
    """`Contado`, `Crédito` o `Crédito N días`; `None` si el XML no la declara.

    SUNAT la informa en `PaymentTerms` (`PaymentMeansID` = Contado/Credito, y
    `CuotaNNN` para las cuotas, que aquí se ignoran). Los días salen de restar la
    emisión al vencimiento cuando ambos existen.
    """
    for termino in _hijos(root, "PaymentTerms"):
        for nombre in ("PaymentMeansID", "ID"):
            valor = (_texto_en(termino, nombre) or "").lower().replace("é", "e")
            if valor == "contado":
                return "Contado"
            if valor == "credito":
                if emision and vencimiento and vencimiento > emision:
                    return f"Crédito {(vencimiento - emision).days} días"
                return "Crédito"
    return None


def _monto_total(root: etree._Element) -> Decimal | None:
    """Total a pagar: `LegalMonetaryTotal` (Invoice/CreditNote) o `RequestedMonetaryTotal`."""
    for contenedor in ("LegalMonetaryTotal", "RequestedMonetaryTotal"):
        total = _texto(_hijo(root, contenedor, "PayableAmount"))
        if total:
            try:
                return Decimal(total)
            except InvalidOperation:
                return None
    return None


# --------------------------------------------------------------------------- #
# Líneas del detalle (ítems)
# --------------------------------------------------------------------------- #
def _decimal(valor: str | None) -> Decimal | None:
    if not valor:
        return None
    try:
        return Decimal(valor)
    except InvalidOperation:
        return None


def _elemento_con_nombre(
    elemento: etree._Element, nombres: tuple[str, ...]
) -> etree._Element | None:
    return next(
        (hijo for hijo in elemento if isinstance(hijo.tag, str) and _localname(hijo) in nombres),
        None,
    )


def _parse_items(root: etree._Element, raiz: str) -> tuple[ItemUbl, ...]:
    """Extrae las líneas del detalle; un XML sin líneas devuelve una tupla vacía."""
    contenedor = _LINEAS_POR_RAIZ[raiz]
    items: list[ItemUbl] = []
    for linea in root:
        if not isinstance(linea.tag, str) or _localname(linea) != contenedor:
            continue
        item = _parse_item(len(items) + 1, linea)
        if item is not None:
            items.append(item)
        if len(items) >= _MAX_ITEMS:
            break
    return tuple(items)


def _parse_item(n: int, linea: etree._Element) -> ItemUbl | None:
    """Convierte una línea UBL en `ItemUbl`; devuelve None si la línea no sirve."""
    cantidad_el = _elemento_con_nombre(linea, _CANTIDADES)
    unidad_medida = cantidad_el.get("unitCode") if cantidad_el is not None else None
    cantidad = _decimal(_texto(cantidad_el))
    if cantidad is not None and cantidad <= 0:
        # Una línea sin cantidad positiva no se puede representar: se descarta
        # en lugar de romper la importación completa.
        return None
    if cantidad is None:
        cantidad = Decimal("1")

    descripcion = _texto(_hijo(linea, "Item", "Description"))
    codigo = _texto(_hijo(linea, "Item", "SellersItemIdentification", "ID"))
    total = _decimal(_texto_en(linea, "LineExtensionAmount"))
    precio_unitario = _precio_unitario(linea)

    valor_unitario = None
    if total is not None:
        valor_unitario = (total / cantidad).quantize(Decimal("0.000001"))

    if descripcion is None and codigo is None and total is None:
        return None

    descuento = _descuento_linea(linea)
    igv, isc, tip_afectacion = _impuestos_linea(linea)

    return ItemUbl(
        item_nro=n,
        descripcion=descripcion or codigo or f"Ítem {n}",
        cantidad=cantidad,
        unidad_medida=unidad_medida,
        codigo=codigo,
        valor_unitario=valor_unitario,
        precio_unitario=precio_unitario,
        descuento=descuento,
        igv=igv,
        isc=isc,
        tip_afectacion_igv=tip_afectacion,
        total=total,
    )


def _precio_unitario(linea: etree._Element) -> Decimal | None:
    """Precio unitario con IGV: `Price/PriceAmount` repartido en `BaseQuantity`."""
    precio = _hijo(linea, "Price")
    monto = _decimal(_texto(_hijo(precio, "PriceAmount")))
    if monto is None:
        return None
    base = _decimal(_texto(_hijo(precio, "BaseQuantity")))
    if base is not None and base > 0:
        return (monto / base).quantize(Decimal("0.000001"))
    return monto


def _descuento_linea(linea: etree._Element) -> Decimal:
    """Suma de los `AllowanceCharge` con `ChargeIndicator=false` de la línea."""
    total = Decimal("0")
    for cargo in linea:
        if not isinstance(cargo.tag, str) or _localname(cargo) != "AllowanceCharge":
            continue
        indicador = (_texto_en(cargo, "ChargeIndicator") or "").lower()
        if indicador not in ("false", "0"):
            continue
        monto = _decimal(_texto_en(cargo, "Amount"))
        if monto is not None:
            total += monto
    return total


def _impuestos_linea(linea: etree._Element) -> tuple[Decimal, Decimal, str | None]:
    """IGV e ISC de la línea, con el tipo de afectación (catálogo 07)."""
    igv = Decimal("0")
    isc = Decimal("0")
    tip_afectacion: str | None = None
    for tax_total in linea:
        if not isinstance(tax_total.tag, str) or _localname(tax_total) != "TaxTotal":
            continue
        for subtotal in tax_total:
            if not isinstance(subtotal.tag, str) or _localname(subtotal) != "TaxSubtotal":
                continue
            monto = _decimal(_texto_en(subtotal, "TaxAmount"))
            if monto is None:
                continue
            esquema = _texto(_hijo(subtotal, "TaxCategory", "TaxScheme", "ID"))
            if esquema == _ESQUEMA_IGV:
                igv += monto
                tip_afectacion = _texto(_hijo(subtotal, "TaxCategory", "ID")) or tip_afectacion
            elif esquema == _ESQUEMA_ISC:
                isc += monto
    return igv, isc, tip_afectacion
