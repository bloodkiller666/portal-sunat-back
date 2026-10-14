"""Firma XMLDSig del comprobante: estructura SUNAT y verificación criptográfica.

El certificado de las pruebas se genera aquí (autofirmado, en un archivo temporal):
el `.pfx` real de la empresa nunca entra al repositorio.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from lxml import etree
from signxml import InvalidDigest, XMLVerifier

from app.core.errors import ConfigError, InvalidDocumentError
from app.integrations.xml_signer import DS_NS, EXT_NS, SunatXmlSigner
from app.secrets.base import CompanyCredentials
from app.secrets.certificate import load_signing_material

UBL = Path(__file__).parent / "fixtures" / "ubl"
CLAVE_CERT = "clave-del-certificado-de-pruebas"
RUC = "20123456789"


def _certificado_autofirmado() -> tuple[object, x509.Certificate]:
    clave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nombre = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ACME SAC (pruebas)")])
    certificado = (
        x509.CertificateBuilder()
        .subject_name(nombre)
        .issuer_name(nombre)
        .public_key(clave.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.now(dt.UTC) - dt.timedelta(days=1))
        .not_valid_after(dt.datetime.now(dt.UTC) + dt.timedelta(days=365))
        .sign(clave, hashes.SHA256())
    )
    return clave, certificado


@pytest.fixture
def credenciales(tmp_path: Path) -> CompanyCredentials:
    clave, certificado = _certificado_autofirmado()
    pfx = pkcs12.serialize_key_and_certificates(
        name=b"acme",
        key=clave,
        cert=certificado,
        cas=None,
        encryption_algorithm=serialization.BestAvailableEncryption(
            CLAVE_CERT.encode("utf-8")
        ),
    )
    archivo = tmp_path / "20123456789.pfx"
    archivo.write_bytes(pfx)
    return CompanyCredentials(
        ruc=RUC,
        sol_user="MODDATOS",
        sol_password="clave-sol-de-pruebas",
        certificate_path=archivo,
        certificate_password=CLAVE_CERT,
    )


def _xml(nombre: str) -> bytes:
    return (UBL / nombre).read_bytes()


def _firma(firmado: bytes) -> etree._Element:
    root = etree.fromstring(firmado)
    firmas = root.xpath("//*[local-name()='Signature']")
    assert len(firmas) == 1
    return firmas[0]


def _extension_content(firmado: bytes) -> etree._Element:
    root = etree.fromstring(firmado)
    contenidos = root.xpath("//*[local-name()='ExtensionContent']")
    assert len(contenidos) == 1
    return contenidos[0]


def test_firma_queda_dentro_de_las_extensiones_ubl(credenciales: CompanyCredentials) -> None:
    firmado = SunatXmlSigner().sign(xml=_xml("factura.xml"), credentials=credenciales)

    contenido = _extension_content(firmado)
    firmas = contenido.xpath("./*[local-name()='Signature']")
    assert len(firmas) == 1
    # Es la primera estructura del documento, como exige UBL 2.1.
    root = etree.fromstring(firmado)
    primer_hijo = next(hijo for hijo in root if isinstance(hijo.tag, str))
    assert primer_hijo.tag == f"{{{EXT_NS}}}UBLExtensions"
    assert firmado.startswith(b"<?xml")


def test_la_firma_es_valida_y_referencia_el_documento_completo(
    credenciales: CompanyCredentials,
) -> None:
    firmado = SunatXmlSigner().sign(xml=_xml("factura.xml"), credentials=credenciales)

    firma = _firma(firmado)
    referencia = firma.xpath("./*[local-name()='SignedInfo']/*[local-name()='Reference']")[0]
    assert referencia.get("URI") == ""
    algoritmos = [t.get("Algorithm") for t in referencia.xpath("./*[local-name()='Transforms']/*")]
    assert "http://www.w3.org/2000/09/xmldsig#enveloped-signature" in algoritmos
    assert firma.nsmap["ds"] == DS_NS

    material = load_signing_material(credenciales)
    verificado = XMLVerifier().verify(firmado, x509_cert=material.certificate)
    assert verificado.signed_xml is not None

    # El certificado de la empresa viaja embebido (KeyInfo/X509Data) en el XML
    # entregado: `signed_xml` es el payload referenciado, sin la firma.
    entregado = etree.fromstring(firmado)
    certificados = entregado.xpath("//*[local-name()='X509Certificate']")
    assert len(certificados) == 1


def test_una_alteracion_del_comprobante_invalida_la_firma(
    credenciales: CompanyCredentials,
) -> None:
    firmado = SunatXmlSigner().sign(xml=_xml("factura.xml"), credentials=credenciales)
    material = load_signing_material(credenciales)

    alterado = firmado.replace(b"F001-00000001", b"F001-00000002")
    with pytest.raises(InvalidDigest):
        XMLVerifier().verify(alterado, x509_cert=material.certificate)


def test_la_firma_no_toca_los_datos_del_comprobante(
    credenciales: CompanyCredentials,
) -> None:
    original = _xml("factura.xml")
    firmado = SunatXmlSigner().sign(xml=original, credentials=credenciales)

    root = etree.fromstring(firmado)
    cbc = "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}"
    assert root.findtext(f"{cbc}ID") == "F001-00000001"
    assert b"20123456789" in firmado
    # Y no se duplica la firma ni se pierde el contenido original.
    assert firmado.count(b"<ds:Signature") + firmado.count(b":Signature") >= 1
    assert len(root.xpath("//*[local-name()='Signature']")) == 1


def test_firma_tambien_las_notas_de_credito(credenciales: CompanyCredentials) -> None:
    firmado = SunatXmlSigner().sign(xml=_xml("nota_credito.xml"), credentials=credenciales)
    material = load_signing_material(credenciales)

    XMLVerifier().verify(firmado, x509_cert=material.certificate)
    assert etree.fromstring(firmado).tag.endswith("CreditNote")


def test_no_se_firma_dos_veces(credenciales: CompanyCredentials) -> None:
    firmado = SunatXmlSigner().sign(xml=_xml("factura.xml"), credentials=credenciales)

    with pytest.raises(InvalidDocumentError) as error:
        SunatXmlSigner().sign(xml=firmado, credentials=credenciales)

    assert "ya tiene una firma" in error.value.message


def test_xml_invalido(credenciales: CompanyCredentials) -> None:
    with pytest.raises(InvalidDocumentError):
        SunatXmlSigner().sign(xml=b"<Invoice", credentials=credenciales)


def test_xml_vacio(credenciales: CompanyCredentials) -> None:
    with pytest.raises(InvalidDocumentError):
        SunatXmlSigner().sign(xml=b"   ", credentials=credenciales)


def test_sin_certificado_configurado() -> None:
    sin_certificado = CompanyCredentials(
        ruc=RUC, sol_user="MODDATOS", sol_password="clave-sol-de-pruebas"
    )

    with pytest.raises(ConfigError) as error:
        SunatXmlSigner().sign(xml=_xml("factura.xml"), credentials=sin_certificado)

    assert "certificado digital" in error.value.message


def test_certificado_publico_sin_clave_privada(tmp_path: Path) -> None:
    """Un .p12 sin clave privada no sirve para firmar."""
    _clave, certificado = _certificado_autofirmado()
    pfx = pkcs12.serialize_key_and_certificates(
        name=b"solo-certificado",
        key=None,
        cert=certificado,
        cas=None,
        encryption_algorithm=serialization.NoEncryption(),
    )
    archivo = tmp_path / "publico.p12"
    archivo.write_bytes(pfx)
    credenciales = CompanyCredentials(
        ruc=RUC, sol_user="MODDATOS", sol_password="x", certificate_path=archivo
    )

    with pytest.raises(ConfigError) as error:
        SunatXmlSigner().sign(xml=_xml("factura.xml"), credentials=credenciales)

    assert "clave privada" in error.value.message


def test_la_clave_del_certificado_se_respeta(tmp_path: Path) -> None:
    clave, certificado = _certificado_autofirmado()
    pfx = pkcs12.serialize_key_and_certificates(
        name=b"acme",
        key=clave,
        cert=certificado,
        cas=None,
        encryption_algorithm=serialization.BestAvailableEncryption(b"otra-clave"),
    )
    archivo = tmp_path / "20123456789.pfx"
    archivo.write_bytes(pfx)
    credenciales = CompanyCredentials(
        ruc=RUC,
        sol_user="MODDATOS",
        sol_password="x",
        certificate_path=archivo,
        certificate_password="clave-equivocada",
    )

    with pytest.raises(ConfigError) as error:
        SunatXmlSigner().sign(xml=_xml("factura.xml"), credentials=credenciales)

    # El mensaje no filtra nada del certificado.
    assert "clave" in error.value.message
    assert "clave-equivocada" not in error.value.message
