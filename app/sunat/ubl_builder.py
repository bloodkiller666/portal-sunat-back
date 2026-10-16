"""Generador del XML UBL 2.1 de facturas y boletas (cara opuesta de `app.sunat.ubl`).

Construye el `Invoice` que SUNAT espera recibir en `sendBill`, **sin firmar**: la
firma la agrega `SunatXmlSigner` en el `ExtensionContent` que este módulo deja vacío.

Alcance (a propósito acotado, ver README):

* Tipos `01` factura y `03` boleta; monedas `PEN` y `USD`.
* Ítems gravados (`10`), exonerados (`20`) e inafectos (`30`) del catálogo 07, con IGV
  del 18 %. Gratuitos, exportación, ISC, detracción y anticipos se rechazan con un
  error claro en vez de generar un XML que SUNAT rechazaría después.
* Pago al contado o a crédito (con cuotas) y leyenda 1000 (monto en letras).

Los importes se calculan aquí con `Decimal` y se redondean línea por línea; los
totales de cabecera son la suma de las líneas ya redondeadas. Así SUNAT nunca ve una
diferencia entre el IGV de las líneas y el de la cabecera (observación 3292/3293).

El orden de los elementos sigue la secuencia del esquema UBL 2.1: SUNAT valida el XML
contra el XSD y un elemento fuera de lugar es rechazo, no advertencia.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, time
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from lxml import etree

from app.core.errors import InvalidDocumentError

NS_INVOICE = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
NS_CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
NS_CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
NS_EXT = "urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2"
NS_DS = "http://www.w3.org/2000/09/xmldsig#"

TASA_IGV = Decimal("0.18")

# Id del `ds:Signature` al que apunta `cac:Signature`; lo fija `SunatXmlSigner`.
ID_FIRMA = "SignatureSP"

_CENTAVOS = Decimal("0.01")
_DIEZ_DECIMALES = Decimal("0.0000000001")
_MAX_ITEMS = 1000

_MONEDAS = {"PEN": "SOLES", "USD": "DOLARES AMERICANOS"}

# Catálogo 07 → (categoría de impuesto, esquema, nombre, código de tipo de tributo).
_AFECTACIONES: dict[str, tuple[str, str, str, str]] = {
    "10": ("S", "1000", "IGV", "VAT"),
    "20": ("E", "9997", "EXO", "VAT"),
    "30": ("O", "9998", "INA", "FRE"),
}

_SERIE_POR_TIPO = {"01": "F", "03": "B"}
_SERIE_RE = re.compile(r"^[FB][A-Z0-9]{3}$")


# --------------------------------------------------------------------------- #
# Entrada
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class EmisorEmision:
    ruc: str
    razon_social: str
    nombre_comercial: str | None = None
    direccion: str | None = None
    ubigeo: str | None = None
    codigo_establecimiento: str = "0000"  # catálogo 30: 0000 = domicilio fiscal


@dataclass(frozen=True, slots=True)
class ReceptorEmision:
    """Adquirente. `tipo_doc` es el catálogo 06: 6 RUC, 1 DNI, 4 CE, 7 pasaporte, 0 sin doc."""

    tipo_doc: str
    numero: str
    razon_social: str


@dataclass(frozen=True, slots=True)
class ItemEmision:
    """Una línea. Se informa `valor_unitario` (sin IGV) o `precio_unitario` (con IGV)."""

    descripcion: str
    cantidad: Decimal
    unidad_medida: str = "NIU"  # catálogo 03: NIU = unidad (bienes), ZZ = servicio
    codigo: str | None = None
    valor_unitario: Decimal | None = None
    precio_unitario: Decimal | None = None
    descuento: Decimal = Decimal("0")  # monto, sobre el valor de venta de la línea
    tip_afectacion_igv: str = "10"


@dataclass(frozen=True, slots=True)
class CuotaEmision:
    monto: Decimal
    fecha_vencimiento: date


@dataclass(frozen=True, slots=True)
class ComprobanteEmision:
    tipo_documento: str
    serie: str
    correlativo: int
    fecha_emision: date
    emisor: EmisorEmision
    receptor: ReceptorEmision
    items: tuple[ItemEmision, ...]
    moneda: str = "PEN"
    hora_emision: time | None = None
    forma_pago: Literal["contado", "credito"] = "contado"
    cuotas: tuple[CuotaEmision, ...] = ()
    fecha_vencimiento: date | None = None
    orden_compra: str | None = None


# --------------------------------------------------------------------------- #
# Salida
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class TotalesEmision:
    gravado: Decimal
    exonerado: Decimal
    inafecto: Decimal
    igv: Decimal
    total: Decimal


@dataclass(frozen=True, slots=True)
class ComprobanteGenerado:
    xml: bytes
    nombre_archivo: str
    totales: TotalesEmision
    leyenda: str = field(default="")


@dataclass(frozen=True, slots=True)
class _Linea:
    n: int
    item: ItemEmision
    valor_unitario: Decimal
    precio_unitario: Decimal
    descuento: Decimal
    base: Decimal  # valor de venta de la línea (sin IGV, ya con descuento)
    igv: Decimal


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def generar_comprobante_xml(comprobante: ComprobanteEmision) -> ComprobanteGenerado:
    """Valida los datos y devuelve el XML UBL 2.1 sin firmar."""
    _validar_cabecera(comprobante)
    lineas = [_calcular_linea(n, item) for n, item in enumerate(comprobante.items, start=1)]
    totales = _calcular_totales(lineas)
    _validar_pago(comprobante, totales)

    leyenda = monto_en_letras(totales.total, comprobante.moneda)
    root = _construir(comprobante, lineas, totales, leyenda)
    xml = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=False)

    nombre = (
        f"{comprobante.emisor.ruc}-{comprobante.tipo_documento}-"
        f"{comprobante.serie}-{comprobante.correlativo:08d}"
    )
    return ComprobanteGenerado(xml=xml, nombre_archivo=nombre, totales=totales, leyenda=leyenda)


def monto_en_letras(monto: Decimal, moneda: str = "PEN") -> str:
    """Leyenda 1000: `SON: CIENTO DIECIOCHO CON 00/100 SOLES`."""
    nombre_moneda = _MONEDAS.get(moneda)
    if nombre_moneda is None:
        raise InvalidDocumentError("Moneda no soportada", details={"moneda": moneda})
    centimos_totales = int((monto.quantize(_CENTAVOS, ROUND_HALF_UP) * 100).to_integral_value())
    entero, centimos = divmod(centimos_totales, 100)
    return f"SON: {_entero_en_letras(entero)} CON {centimos:02d}/100 {nombre_moneda}"


# --------------------------------------------------------------------------- #
# Validación y cálculo
# --------------------------------------------------------------------------- #
def _error(mensaje: str, **detalles: object) -> InvalidDocumentError:
    return InvalidDocumentError(mensaje, details=detalles or None)


def _validar_cabecera(c: ComprobanteEmision) -> None:
    if c.tipo_documento not in _SERIE_POR_TIPO:
        raise _error(
            "Solo se generan facturas (01) y boletas (03)", tipo_documento=c.tipo_documento
        )
    if not _SERIE_RE.match(c.serie) or c.serie[0] != _SERIE_POR_TIPO[c.tipo_documento]:
        raise _error(
            "La serie no corresponde al tipo (factura: F###, boleta: B###)",
            serie=c.serie,
            tipo_documento=c.tipo_documento,
        )
    if not 1 <= c.correlativo <= 99_999_999:
        raise _error("El correlativo debe estar entre 1 y 99999999", correlativo=c.correlativo)
    if c.moneda not in _MONEDAS:
        raise _error("Moneda no soportada (PEN o USD)", moneda=c.moneda)
    if not re.fullmatch(r"[0-9]{11}", c.emisor.ruc):
        raise _error("El RUC del emisor debe tener 11 dígitos", ruc=c.emisor.ruc)
    if not c.emisor.razon_social.strip():
        raise _error("El emisor no tiene razón social")
    if not 1 <= len(c.items) <= _MAX_ITEMS:
        raise _error(f"El comprobante debe tener entre 1 y {_MAX_ITEMS} ítems")
    _validar_receptor(c)


def _validar_receptor(c: ComprobanteEmision) -> None:
    r = c.receptor
    patrones = {"6": r"[0-9]{11}", "1": r"[0-9]{8}"}
    if c.tipo_documento == "01" and r.tipo_doc != "6":
        raise _error("La factura exige el RUC del adquirente", tipo_doc=r.tipo_doc)
    if r.tipo_doc not in {"6", "1", "4", "7", "0"}:
        raise _error("Tipo de documento del adquirente no soportado", tipo_doc=r.tipo_doc)
    if r.tipo_doc in patrones and not re.fullmatch(patrones[r.tipo_doc], r.numero):
        raise _error(
            "El número de documento del adquirente no tiene el formato del tipo indicado",
            tipo_doc=r.tipo_doc,
            numero=r.numero,
        )
    if r.tipo_doc == "0" and r.numero != "-":
        raise _error("Sin documento, el número del adquirente debe ser '-'")
    if not r.razon_social.strip():
        raise _error("El adquirente no tiene nombre o razón social")


def _q2(valor: Decimal) -> Decimal:
    return valor.quantize(_CENTAVOS, ROUND_HALF_UP)


def _calcular_linea(n: int, item: ItemEmision) -> _Linea:
    if item.tip_afectacion_igv not in _AFECTACIONES:
        raise _error(
            "Tipo de afectación al IGV no soportado (solo 10, 20 y 30)",
            item=n,
            tip_afectacion_igv=item.tip_afectacion_igv,
        )
    if not item.descripcion.strip():
        raise _error("El ítem no tiene descripción", item=n)
    if item.cantidad <= 0:
        raise _error("La cantidad del ítem debe ser mayor que cero", item=n)
    if item.descuento < 0:
        raise _error("El descuento del ítem no puede ser negativo", item=n)

    factor = 1 + TASA_IGV if item.tip_afectacion_igv == "10" else Decimal(1)
    if item.valor_unitario is not None:
        valor_unitario = item.valor_unitario
    elif item.precio_unitario is not None:
        valor_unitario = (item.precio_unitario / factor).quantize(_DIEZ_DECIMALES, ROUND_HALF_UP)
    else:
        raise _error("El ítem necesita valor_unitario o precio_unitario", item=n)
    if valor_unitario < 0:
        raise _error("El valor unitario no puede ser negativo", item=n)

    base = _q2(item.cantidad * valor_unitario - item.descuento)
    if base < 0:
        raise _error("El descuento supera el valor de venta del ítem", item=n)
    igv = _q2(base * TASA_IGV) if item.tip_afectacion_igv == "10" else Decimal("0.00")
    precio_unitario = (
        item.precio_unitario
        if item.precio_unitario is not None
        else (valor_unitario * factor).quantize(_DIEZ_DECIMALES, ROUND_HALF_UP)
    )
    return _Linea(
        n=n,
        item=item,
        valor_unitario=valor_unitario,
        precio_unitario=precio_unitario,
        descuento=_q2(item.descuento),
        base=base,
        igv=igv,
    )


def _calcular_totales(lineas: list[_Linea]) -> TotalesEmision:
    def suma(afectacion: str) -> Decimal:
        return sum(
            (linea.base for linea in lineas if linea.item.tip_afectacion_igv == afectacion),
            Decimal("0.00"),
        )

    igv = sum((linea.igv for linea in lineas), Decimal("0.00"))
    gravado, exonerado, inafecto = suma("10"), suma("20"), suma("30")
    return TotalesEmision(
        gravado=gravado,
        exonerado=exonerado,
        inafecto=inafecto,
        igv=igv,
        total=gravado + exonerado + inafecto + igv,
    )


def _validar_pago(c: ComprobanteEmision, totales: TotalesEmision) -> None:
    if c.forma_pago == "contado":
        if c.cuotas:
            raise _error("Un pago al contado no lleva cuotas")
        return
    if c.tipo_documento == "03":
        raise _error("La boleta de venta se emite solo al contado")
    if not c.cuotas:
        raise _error("El pago a crédito necesita al menos una cuota")
    suma = sum((_q2(cuota.monto) for cuota in c.cuotas), Decimal("0.00"))
    if suma != totales.total:
        raise _error(
            "La suma de las cuotas debe ser igual al total del comprobante",
            suma_cuotas=str(suma),
            total=str(totales.total),
        )
    anterior = c.fecha_emision
    for cuota in c.cuotas:
        if cuota.fecha_vencimiento < anterior:
            raise _error("Las cuotas deben vencer en orden y no antes de la emisión")
        anterior = cuota.fecha_vencimiento


# --------------------------------------------------------------------------- #
# Construcción del XML
# --------------------------------------------------------------------------- #
def _tag(ns: str, nombre: str) -> str:
    return f"{{{ns}}}{nombre}"


def _el(
    padre: etree._Element,
    ns: str,
    nombre: str,
    texto: str | None = None,
    *,
    cdata: bool = False,
    **atributos: str,
) -> etree._Element:
    elemento = etree.SubElement(padre, _tag(ns, nombre), **atributos)
    if texto is not None:
        elemento.text = etree.CDATA(texto) if cdata else texto
    return elemento


def _cbc(padre, nombre, texto=None, **atributos):
    return _el(padre, NS_CBC, nombre, texto, **atributos)


def _cac(padre, nombre):
    return _el(padre, NS_CAC, nombre)


def _monto(valor: Decimal) -> str:
    return f"{_q2(valor):.2f}"


def _unitario(valor: Decimal) -> str:
    """Hasta 10 decimales (límite de SUNAT) y mínimo 2, sin ceros de relleno."""
    texto = f"{valor.quantize(_DIEZ_DECIMALES, ROUND_HALF_UP):f}".rstrip("0")
    entero, _, decimales = texto.partition(".")
    return f"{entero}.{decimales.ljust(2, '0')}"


def _construir(
    c: ComprobanteEmision, lineas: list[_Linea], totales: TotalesEmision, leyenda: str
) -> etree._Element:
    root = etree.Element(
        _tag(NS_INVOICE, "Invoice"),
        nsmap={None: NS_INVOICE, "cac": NS_CAC, "cbc": NS_CBC, "ds": NS_DS, "ext": NS_EXT},
    )

    # Hueco vacío para la firma: `SunatXmlSigner` coloca ahí el `ds:Signature`.
    extensiones = etree.SubElement(root, _tag(NS_EXT, "UBLExtensions"))
    extension = etree.SubElement(extensiones, _tag(NS_EXT, "UBLExtension"))
    etree.SubElement(extension, _tag(NS_EXT, "ExtensionContent"))

    _cbc(root, "UBLVersionID", "2.1")
    _cbc(root, "CustomizationID", "2.0")
    # Mismo formato que el nombre del archivo: SUNAT los compara.
    _cbc(root, "ID", f"{c.serie}-{c.correlativo:08d}")
    _cbc(root, "IssueDate", c.fecha_emision.isoformat())
    if c.hora_emision is not None:
        _cbc(root, "IssueTime", c.hora_emision.replace(microsecond=0).isoformat())
    if c.fecha_vencimiento is not None:
        _cbc(root, "DueDate", c.fecha_vencimiento.isoformat())
    _cbc(
        root,
        "InvoiceTypeCode",
        c.tipo_documento,
        listID="0101",  # catálogo 51: venta interna
        listAgencyName="PE:SUNAT",
        listName="Tipo de Documento",
        listURI="urn:pe:gob:sunat:cpe:see:gem:catalogos:catalogo01",
    )
    _cbc(root, "Note", languageLocaleID="1000").text = etree.CDATA(leyenda)
    _cbc(root, "DocumentCurrencyCode", c.moneda)
    _cbc(root, "LineCountNumeric", str(len(lineas)))
    if c.orden_compra:
        orden = _cac(root, "OrderReference")
        _cbc(orden, "ID", c.orden_compra)

    _firma_referencia(root, c.emisor)
    _emisor(root, c.emisor)
    _receptor(root, c.receptor)
    _terminos_de_pago(root, c, totales)
    _impuestos_cabecera(root, c.moneda, totales)

    monetario = _cac(root, "LegalMonetaryTotal")
    base = totales.gravado + totales.exonerado + totales.inafecto
    _cbc(monetario, "LineExtensionAmount", _monto(base), currencyID=c.moneda)
    _cbc(monetario, "TaxInclusiveAmount", _monto(totales.total), currencyID=c.moneda)
    _cbc(monetario, "PayableAmount", _monto(totales.total), currencyID=c.moneda)

    for linea in lineas:
        _linea(root, c.moneda, linea)
    return root


def _firma_referencia(root: etree._Element, emisor: EmisorEmision) -> None:
    """`cac:Signature`: identifica al firmante y apunta al `ds:Signature` por su Id."""
    firma = _cac(root, "Signature")
    _cbc(firma, "ID", emisor.ruc)
    firmante = _cac(firma, "SignatoryParty")
    _cbc(_cac(firmante, "PartyIdentification"), "ID", emisor.ruc)
    _cbc(_cac(firmante, "PartyName"), "Name").text = etree.CDATA(emisor.razon_social)
    adjunto = _cac(_cac(firma, "DigitalSignatureAttachment"), "ExternalReference")
    _cbc(adjunto, "URI", f"#{ID_FIRMA}")


def _identificacion(party: etree._Element, tipo_doc: str, numero: str) -> None:
    _cbc(
        _cac(party, "PartyIdentification"),
        "ID",
        numero,
        schemeID=tipo_doc,
        schemeName="Documento de Identidad",
        schemeAgencyName="PE:SUNAT",
        schemeURI="urn:pe:gob:sunat:cpe:see:gem:catalogos:catalogo06",
    )


def _emisor(root: etree._Element, emisor: EmisorEmision) -> None:
    party = _cac(_cac(root, "AccountingSupplierParty"), "Party")
    _identificacion(party, "6", emisor.ruc)
    if emisor.nombre_comercial:
        _cbc(_cac(party, "PartyName"), "Name").text = etree.CDATA(emisor.nombre_comercial)
    legal = _cac(party, "PartyLegalEntity")
    _cbc(legal, "RegistrationName").text = etree.CDATA(emisor.razon_social)
    direccion = _cac(legal, "RegistrationAddress")
    if emisor.ubigeo:
        _cbc(direccion, "ID", emisor.ubigeo)
    _cbc(direccion, "AddressTypeCode", emisor.codigo_establecimiento)
    if emisor.direccion:
        _cbc(_cac(direccion, "AddressLine"), "Line").text = etree.CDATA(emisor.direccion)
    _cbc(_cac(direccion, "Country"), "IdentificationCode", "PE")


def _receptor(root: etree._Element, receptor: ReceptorEmision) -> None:
    party = _cac(_cac(root, "AccountingCustomerParty"), "Party")
    _identificacion(party, receptor.tipo_doc, receptor.numero)
    _cbc(_cac(party, "PartyLegalEntity"), "RegistrationName").text = etree.CDATA(
        receptor.razon_social
    )


def _terminos_de_pago(root: etree._Element, c: ComprobanteEmision, totales: TotalesEmision) -> None:
    if c.forma_pago == "contado":
        termino = _cac(root, "PaymentTerms")
        _cbc(termino, "ID", "FormaPago")
        _cbc(termino, "PaymentMeansID", "Contado")
        return

    termino = _cac(root, "PaymentTerms")
    _cbc(termino, "ID", "FormaPago")
    _cbc(termino, "PaymentMeansID", "Credito")
    _cbc(termino, "Amount", _monto(totales.total), currencyID=c.moneda)
    for n, cuota in enumerate(c.cuotas, start=1):
        termino = _cac(root, "PaymentTerms")
        _cbc(termino, "ID", "FormaPago")
        _cbc(termino, "PaymentMeansID", f"Cuota{n:03d}")
        _cbc(termino, "Amount", _monto(cuota.monto), currencyID=c.moneda)
        _cbc(termino, "PaymentDueDate", cuota.fecha_vencimiento.isoformat())


def _categoria_impuesto(padre: etree._Element, afectacion: str, *, linea: bool) -> None:
    categoria_id, esquema, nombre, tipo = _AFECTACIONES[afectacion]
    categoria = _cac(padre, "TaxCategory")
    _cbc(categoria, "ID", categoria_id)
    if linea:
        if afectacion == "10":
            _cbc(categoria, "Percent", "18.00")
        _cbc(categoria, "TaxExemptionReasonCode", afectacion)
    esquema_el = _cac(categoria, "TaxScheme")
    _cbc(esquema_el, "ID", esquema)
    _cbc(esquema_el, "Name", nombre)
    _cbc(esquema_el, "TaxTypeCode", tipo)


def _impuestos_cabecera(root: etree._Element, moneda: str, totales: TotalesEmision) -> None:
    total = _cac(root, "TaxTotal")
    _cbc(total, "TaxAmount", _monto(totales.igv), currencyID=moneda)
    por_afectacion = [
        (afectacion, base)
        for afectacion, base in (
            ("10", totales.gravado),
            ("20", totales.exonerado),
            ("30", totales.inafecto),
        )
        if base > 0
    ]
    # SUNAT exige al menos un TaxSubtotal aunque todo valga cero.
    for afectacion, base in por_afectacion or [("10", totales.gravado)]:
        subtotal = _cac(total, "TaxSubtotal")
        _cbc(subtotal, "TaxableAmount", _monto(base), currencyID=moneda)
        impuesto = totales.igv if afectacion == "10" else Decimal("0.00")
        _cbc(subtotal, "TaxAmount", _monto(impuesto), currencyID=moneda)
        _categoria_impuesto(subtotal, afectacion, linea=False)


def _linea(root: etree._Element, moneda: str, linea: _Linea) -> None:
    item = linea.item
    el = _cac(root, "InvoiceLine")
    _cbc(el, "ID", str(linea.n))
    _cbc(
        el,
        "InvoicedQuantity",
        _unitario(item.cantidad),
        unitCode=item.unidad_medida,
        unitCodeListID="UN/ECE rec 20",
        unitCodeListAgencyName="United Nations Economic Commission for Europe",
    )
    _cbc(el, "LineExtensionAmount", _monto(linea.base), currencyID=moneda)

    referencia = _cac(el, "PricingReference")
    alternativo = _cac(referencia, "AlternativeConditionPrice")
    _cbc(alternativo, "PriceAmount", _unitario(linea.precio_unitario), currencyID=moneda)
    _cbc(alternativo, "PriceTypeCode", "01")  # catálogo 16: precio unitario (incluye IGV)

    if linea.descuento > 0:
        cargo = _cac(el, "AllowanceCharge")
        _cbc(cargo, "ChargeIndicator", "false")
        _cbc(cargo, "AllowanceChargeReasonCode", "00")  # catálogo 53: descuento que afecta la base
        bruto = linea.base + linea.descuento
        _cbc(cargo, "MultiplierFactorNumeric", f"{linea.descuento / bruto:.5f}" if bruto else "0")
        _cbc(cargo, "Amount", _monto(linea.descuento), currencyID=moneda)
        _cbc(cargo, "BaseAmount", _monto(bruto), currencyID=moneda)

    impuestos = _cac(el, "TaxTotal")
    _cbc(impuestos, "TaxAmount", _monto(linea.igv), currencyID=moneda)
    subtotal = _cac(impuestos, "TaxSubtotal")
    _cbc(subtotal, "TaxableAmount", _monto(linea.base), currencyID=moneda)
    _cbc(subtotal, "TaxAmount", _monto(linea.igv), currencyID=moneda)
    _categoria_impuesto(subtotal, item.tip_afectacion_igv, linea=True)

    producto = _cac(el, "Item")
    _cbc(producto, "Description").text = etree.CDATA(item.descripcion)
    if item.codigo:
        _cbc(_cac(producto, "SellersItemIdentification"), "ID", item.codigo)
    _cbc(_cac(el, "Price"), "PriceAmount", _unitario(linea.valor_unitario), currencyID=moneda)


# --------------------------------------------------------------------------- #
# Número en letras (leyenda 1000)
# --------------------------------------------------------------------------- #
_UNIDADES = [
    "CERO", "UNO", "DOS", "TRES", "CUATRO", "CINCO", "SEIS", "SIETE", "OCHO", "NUEVE", "DIEZ",
    "ONCE", "DOCE", "TRECE", "CATORCE", "QUINCE", "DIECISEIS", "DIECISIETE", "DIECIOCHO",
    "DIECINUEVE", "VEINTE", "VEINTIUNO", "VEINTIDOS", "VEINTITRES", "VEINTICUATRO",
    "VEINTICINCO", "VEINTISEIS", "VEINTISIETE", "VEINTIOCHO", "VEINTINUEVE",
]
_DECENAS = ["TREINTA", "CUARENTA", "CINCUENTA", "SESENTA", "SETENTA", "OCHENTA", "NOVENTA"]
_CENTENAS = [
    "CIENTO", "DOSCIENTOS", "TRESCIENTOS", "CUATROCIENTOS", "QUINIENTOS", "SEISCIENTOS",
    "SETECIENTOS", "OCHOCIENTOS", "NOVECIENTOS",
]


def _entero_en_letras(n: int) -> str:
    if n < 30:
        return _UNIDADES[n]
    if n < 100:
        decenas, unidad = divmod(n, 10)
        base = _DECENAS[decenas - 3]
        return base if unidad == 0 else f"{base} Y {_UNIDADES[unidad]}"
    if n == 100:
        return "CIEN"
    if n < 1000:
        centenas, resto = divmod(n, 100)
        return _CENTENAS[centenas - 1] + ("" if resto == 0 else f" {_entero_en_letras(resto)}")
    if n < 1_000_000:
        miles, resto = divmod(n, 1000)
        prefijo = "MIL" if miles == 1 else f"{_apocopar(_entero_en_letras(miles))} MIL"
        return prefijo + ("" if resto == 0 else f" {_entero_en_letras(resto)}")
    if n < 1_000_000_000_000:
        millones, resto = divmod(n, 1_000_000)
        prefijo = (
            "UN MILLON"
            if millones == 1
            else f"{_apocopar(_entero_en_letras(millones))} MILLONES"
        )
        return prefijo + ("" if resto == 0 else f" {_entero_en_letras(resto)}")
    raise InvalidDocumentError("El monto es demasiado grande para la leyenda en letras")


def _apocopar(texto: str) -> str:
    """`UNO` → `UN` delante de MIL/MILLONES (`VEINTIUN MIL`, `TREINTA Y UN MIL`)."""
    return texto[:-1] if texto.endswith("UNO") else texto
