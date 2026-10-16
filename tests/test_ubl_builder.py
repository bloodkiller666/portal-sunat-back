"""Generador UBL 2.1: cálculo de importes, estructura SUNAT, ida y vuelta y firma."""

from __future__ import annotations

import datetime as dt
from datetime import date, time
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from lxml import etree
from signxml import XMLVerifier

from app.core.errors import InvalidDocumentError
from app.integrations import xml_signer
from app.integrations.xml_signer import SunatXmlSigner
from app.secrets.base import CompanyCredentials
from app.sunat import ubl_builder
from app.sunat.ubl import parse_comprobante_ubl
from app.sunat.ubl_builder import (
    ComprobanteEmision,
    CuotaEmision,
    EmisorEmision,
    ItemEmision,
    ReceptorEmision,
    generar_comprobante_xml,
    monto_en_letras,
)

NS = {
    "inv": ubl_builder.NS_INVOICE,
    "cac": ubl_builder.NS_CAC,
    "cbc": ubl_builder.NS_CBC,
}
EMISOR = EmisorEmision(
    ruc="20514831620",
    razon_social="TDM ASFALTOS S.A.C.",
    direccion="AV. EJEMPLO 123",
    ubigeo="150101",
)
CLIENTE = ReceptorEmision(tipo_doc="6", numero="20987654321", razon_social="CLIENTE S.A.C.")


def _factura(**cambios) -> ComprobanteEmision:
    datos = {
        "tipo_documento": "01",
        "serie": "F001",
        "correlativo": 7,
        "fecha_emision": date(2026, 10, 5),
        "emisor": EMISOR,
        "receptor": CLIENTE,
        "items": (
            ItemEmision(
                descripcion="Asfalto RC-250",
                cantidad=Decimal("2"),
                valor_unitario=Decimal("100"),
                codigo="ASF-01",
            ),
        ),
    }
    datos.update(cambios)
    return ComprobanteEmision(**datos)


def _raiz(generado) -> etree._Element:
    return etree.fromstring(generado.xml)


def _texto(raiz: etree._Element, xpath: str) -> str:
    return raiz.xpath(xpath, namespaces=NS)[0].text


# ---------------------------------------------------------------- importes
def test_factura_gravada_calcula_igv_y_total() -> None:
    generado = generar_comprobante_xml(_factura())

    assert generado.totales.gravado == Decimal("200.00")
    assert generado.totales.igv == Decimal("36.00")
    assert generado.totales.total == Decimal("236.00")
    assert generado.nombre_archivo == "20514831620-01-F001-00000007"
    assert generado.leyenda == "SON: DOSCIENTOS TREINTA Y SEIS CON 00/100 SOLES"


def test_precio_con_igv_deriva_el_valor_unitario() -> None:
    item = ItemEmision(
        descripcion="Servicio", cantidad=Decimal("1"), precio_unitario=Decimal("118")
    )
    generado = generar_comprobante_xml(_factura(items=(item,)))

    assert generado.totales.gravado == Decimal("100.00")
    assert generado.totales.igv == Decimal("18.00")


def test_descuento_reduce_la_base_y_se_declara_en_la_linea() -> None:
    item = ItemEmision(
        descripcion="Cemento",
        cantidad=Decimal("2"),
        valor_unitario=Decimal("50"),
        descuento=Decimal("10"),
    )
    generado = generar_comprobante_xml(_factura(items=(item,)))
    raiz = _raiz(generado)

    assert generado.totales.gravado == Decimal("90.00")
    assert _texto(raiz, "//cac:AllowanceCharge/cbc:Amount") == "10.00"
    assert _texto(raiz, "//cac:AllowanceCharge/cbc:BaseAmount") == "100.00"


def test_la_cabecera_suma_las_lineas_ya_redondeadas() -> None:
    # 3 líneas de 0.05: el IGV de cada una redondea a 0.01 (0.009 → 0.01).
    items = tuple(
        ItemEmision(
            descripcion=f"Tornillo {n}", cantidad=Decimal("1"), valor_unitario=Decimal("0.05")
        )
        for n in range(3)
    )
    generado = generar_comprobante_xml(_factura(items=items))
    raiz = _raiz(generado)

    igv_lineas = sum(
        Decimal(e.text)
        for e in raiz.xpath("//cac:InvoiceLine/cac:TaxTotal/cbc:TaxAmount", namespaces=NS)
    )
    assert generado.totales.igv == igv_lineas == Decimal("0.03")


def test_mezcla_de_afectaciones_genera_un_subtotal_por_categoria() -> None:
    items = (
        ItemEmision(descripcion="Gravado", cantidad=Decimal(1), valor_unitario=Decimal("100")),
        ItemEmision(
            descripcion="Exonerado",
            cantidad=Decimal(1),
            valor_unitario=Decimal("50"),
            tip_afectacion_igv="20",
        ),
    )
    generado = generar_comprobante_xml(_factura(items=items))
    raiz = _raiz(generado)

    assert generado.totales.exonerado == Decimal("50.00")
    assert generado.totales.total == Decimal("168.00")
    esquemas = raiz.xpath(
        "/inv:Invoice/cac:TaxTotal/cac:TaxSubtotal//cac:TaxScheme/cbc:ID/text()", namespaces=NS
    )
    assert esquemas == ["1000", "9997"]


# --------------------------------------------------------------- estructura
def test_estructura_de_cabecera_segun_sunat() -> None:
    generado = generar_comprobante_xml(_factura(hora_emision=time(14, 30, 5)))
    raiz = _raiz(generado)

    hijos = [etree.QName(h).localname for h in raiz]
    # Orden del esquema UBL 2.1: una violación es rechazo de SUNAT.
    assert hijos[:12] == [
        "UBLExtensions",
        "UBLVersionID",
        "CustomizationID",
        "ID",
        "IssueDate",
        "IssueTime",
        "InvoiceTypeCode",
        "Note",
        "DocumentCurrencyCode",
        "LineCountNumeric",
        "Signature",
        "AccountingSupplierParty",
    ]
    assert (
        hijos[-3:] == ["LegalMonetaryTotal", "InvoiceLine", "InvoiceLine"][-3:]
        or hijos[-1] == "InvoiceLine"
    )
    assert _texto(raiz, "/inv:Invoice/cbc:ID") == "F001-00000007"
    assert _texto(raiz, "/inv:Invoice/cbc:IssueTime") == "14:30:05"
    assert _texto(raiz, "/inv:Invoice/cbc:InvoiceTypeCode") == "01"
    assert raiz.xpath("/inv:Invoice/cbc:Note/@languageLocaleID", namespaces=NS) == ["1000"]
    assert raiz.xpath(
        "//cac:Signature/cac:DigitalSignatureAttachment//cbc:URI/text()", namespaces=NS
    ) == ["#SignatureSP"]


def test_el_xml_generado_se_lee_con_el_parser_de_importacion() -> None:
    items = (
        ItemEmision(
            descripcion="Asfalto RC-250",
            cantidad=Decimal("2"),
            valor_unitario=Decimal("100"),
            codigo="ASF-01",
            unidad_medida="KGM",
        ),
    )
    generado = generar_comprobante_xml(_factura(items=items))

    leido = parse_comprobante_ubl(generado.xml, ruc_esperado="20514831620")

    assert (leido.tipo_documento, leido.serie, leido.correlativo) == ("01", "F001", 7)
    assert leido.monto_total == Decimal("236.00")
    assert leido.razon_social_receptor == "CLIENTE S.A.C."
    assert leido.ruc_receptor == "20987654321"
    assert leido.nombre_archivo == generado.nombre_archivo
    (item,) = leido.items
    assert item.cantidad == Decimal("2.00")
    assert item.unidad_medida == "KGM"
    assert item.codigo == "ASF-01"
    assert item.igv == Decimal("36.00")
    assert item.tip_afectacion_igv == "10"
    assert item.precio_unitario == Decimal("118.00")


def test_boleta_con_dni() -> None:
    boleta = _factura(
        tipo_documento="03",
        serie="B001",
        receptor=ReceptorEmision(tipo_doc="1", numero="12345678", razon_social="JUAN PEREZ"),
    )
    generado = generar_comprobante_xml(boleta)

    assert generado.nombre_archivo.endswith("-03-B001-00000007")
    assert parse_comprobante_ubl(generado.xml).tipo_documento == "03"


def test_pago_a_credito_declara_cuotas_y_vencimiento() -> None:
    cuotas = (
        CuotaEmision(monto=Decimal("100"), fecha_vencimiento=date(2026, 11, 4)),
        CuotaEmision(monto=Decimal("136"), fecha_vencimiento=date(2026, 12, 4)),
    )
    generado = generar_comprobante_xml(_factura(forma_pago="credito", cuotas=cuotas))
    leido = parse_comprobante_ubl(generado.xml)

    assert leido.forma_pago == "Crédito 60 días"
    assert leido.fecha_vencimiento == date(2026, 12, 4)


def test_caracteres_especiales_en_textos_se_escapan() -> None:
    item = ItemEmision(
        descripcion='Tubo 1/2" <PVC> & accesorios',
        cantidad=Decimal(1),
        valor_unitario=Decimal(10),
    )
    generado = generar_comprobante_xml(_factura(items=(item,)))

    assert parse_comprobante_ubl(generado.xml).items[0].descripcion == item.descripcion


# ---------------------------------------------------------------- validación
@pytest.mark.parametrize(
    "cambios",
    [
        {"tipo_documento": "07"},
        {"serie": "B001"},  # serie de boleta en una factura
        {"serie": "F1"},
        {"correlativo": 0},
        {"correlativo": 100_000_000},
        {"moneda": "EUR"},
        {"items": ()},
        {"receptor": ReceptorEmision("1", "12345678", "JUAN")},  # factura exige RUC
        {"receptor": ReceptorEmision("6", "123", "CLIENTE")},
        {"forma_pago": "credito"},  # sin cuotas
        {"forma_pago": "credito", "cuotas": (CuotaEmision(Decimal("1"), date(2026, 11, 1)),)},
        {"cuotas": (CuotaEmision(Decimal("236"), date(2026, 11, 1)),)},  # contado con cuotas
    ],
)
def test_datos_invalidos_se_rechazan(cambios: dict) -> None:
    with pytest.raises(InvalidDocumentError):
        generar_comprobante_xml(_factura(**cambios))


@pytest.mark.parametrize(
    "item",
    [
        ItemEmision(descripcion="x", cantidad=Decimal(0), valor_unitario=Decimal(1)),
        ItemEmision(descripcion="x", cantidad=Decimal(1)),  # sin precio
        ItemEmision(
            descripcion="x", cantidad=Decimal(1), valor_unitario=Decimal(1), descuento=Decimal(5)
        ),
        ItemEmision(
            descripcion="x", cantidad=Decimal(1), valor_unitario=Decimal(1), tip_afectacion_igv="11"
        ),
        ItemEmision(descripcion=" ", cantidad=Decimal(1), valor_unitario=Decimal(1)),
    ],
)
def test_items_invalidos_se_rechazan(item: ItemEmision) -> None:
    with pytest.raises(InvalidDocumentError):
        generar_comprobante_xml(_factura(items=(item,)))


def test_la_boleta_no_admite_credito() -> None:
    cuotas = (CuotaEmision(Decimal("236"), date(2026, 11, 1)),)
    with pytest.raises(InvalidDocumentError):
        generar_comprobante_xml(
            _factura(
                tipo_documento="03",
                serie="B001",
                receptor=ReceptorEmision("0", "-", "CLIENTE VARIOS"),
                forma_pago="credito",
                cuotas=cuotas,
            )
        )


# -------------------------------------------------------------------- letras
@pytest.mark.parametrize(
    ("monto", "esperado"),
    [
        ("0", "SON: CERO CON 00/100 SOLES"),
        ("1", "SON: UNO CON 00/100 SOLES"),
        ("21.50", "SON: VEINTIUNO CON 50/100 SOLES"),
        ("100", "SON: CIEN CON 00/100 SOLES"),
        ("118.00", "SON: CIENTO DIECIOCHO CON 00/100 SOLES"),
        ("1000", "SON: MIL CON 00/100 SOLES"),
        ("21000", "SON: VEINTIUN MIL CON 00/100 SOLES"),
        ("31001.05", "SON: TREINTA Y UN MIL UNO CON 05/100 SOLES"),
        ("1500000", "SON: UN MILLON QUINIENTOS MIL CON 00/100 SOLES"),
        ("2000000", "SON: DOS MILLONES CON 00/100 SOLES"),
        ("0.995", "SON: UNO CON 00/100 SOLES"),
    ],
)
def test_monto_en_letras(monto: str, esperado: str) -> None:
    assert monto_en_letras(Decimal(monto)) == esperado


def test_monto_en_letras_dolares() -> None:
    assert monto_en_letras(Decimal("5"), "USD") == "SON: CINCO CON 00/100 DOLARES AMERICANOS"


# --------------------------------------------------------------------- firma
def test_el_id_de_firma_coincide_entre_generador_y_firmador() -> None:
    assert ubl_builder.ID_FIRMA == xml_signer.ID_FIRMA


@pytest.fixture
def credenciales(tmp_path: Path) -> CompanyCredentials:
    clave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nombre = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TDM (pruebas)")])
    certificado = (
        x509.CertificateBuilder()
        .subject_name(nombre)
        .issuer_name(nombre)
        .public_key(clave.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.now(dt.UTC) - dt.timedelta(days=1))
        .not_valid_after(dt.datetime.now(dt.UTC) + dt.timedelta(days=30))
        .sign(clave, hashes.SHA256())
    )
    pfx = pkcs12.serialize_key_and_certificates(
        name=b"tdm",
        key=clave,
        cert=certificado,
        cas=None,
        encryption_algorithm=serialization.BestAvailableEncryption(b"clave-de-pruebas"),
    )
    archivo = tmp_path / "20514831620.pfx"
    archivo.write_bytes(pfx)
    return CompanyCredentials(
        ruc="20514831620",
        sol_user="MODDATOS",
        sol_password="clave-sol-de-pruebas",
        certificate_path=archivo,
        certificate_password="clave-de-pruebas",
    )


def test_el_xml_generado_se_firma_y_la_firma_verifica(credenciales: CompanyCredentials) -> None:
    generado = generar_comprobante_xml(_factura())

    firmado = SunatXmlSigner().sign(xml=generado.xml, credentials=credenciales)
    raiz = etree.fromstring(firmado)

    ds = "http://www.w3.org/2000/09/xmldsig#"
    firmas = raiz.xpath("//ds:Signature", namespaces={"ds": ds})
    assert len(firmas) == 1
    assert firmas[0].get("Id") == "SignatureSP"
    # La firma digital está en las extensiones; el cac:Signature sigue en su sitio.
    assert raiz.xpath(
        "//ext:ExtensionContent/ds:Signature", namespaces={"ds": ds, "ext": ubl_builder.NS_EXT}
    )
    assert raiz.xpath("/inv:Invoice/cac:Signature", namespaces=NS)

    resultado = XMLVerifier().verify(firmado, x509_cert=_certificado_embebido(raiz, ds))
    assert resultado.signed_xml is not None


def _certificado_embebido(raiz: etree._Element, ds: str) -> bytes:
    base64_cert = raiz.xpath("//ds:X509Certificate", namespaces={"ds": ds})[0].text.strip()
    return (
        "-----BEGIN CERTIFICATE-----\n" + base64_cert + "\n-----END CERTIFICATE-----\n"
    ).encode()
