"""Firma digital del XML UBL 2.1 (XMLDSig enveloped).

SUNAT exige que la firma viaje **dentro** del comprobante:

```xml
<Invoice>
  <ext:UBLExtensions>
    <ext:UBLExtension>
      <ext:ExtensionContent>
        <ds:Signature>…</ds:Signature>
      </ext:ExtensionContent>
    </ext:UBLExtension>
  </ext:UBLExtensions>
  …
```

La firma referencia el documento completo (`ds:Reference URI=""`) con la
transformación *enveloped-signature*, algoritmo `rsa-sha256` y digest `sha-256`,
y lleva el certificado embebido. El certificado y su clave privada salen del
`SecretsProvider` por empresa (nunca del repositorio).

`signxml` no sabe colocar la firma en `ExtensionContent`, así que se firma el
documento raíz y después se mueve el `ds:Signature` ya calculado a su lugar: el
digest no cambia porque la transformación enveloped descarta el elemento firma
esté donde esté (verificado con `signxml.XMLVerifier` en las pruebas).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from lxml import etree

from app.core.errors import ConfigError, InvalidDocumentError
from app.core.logging import get_logger
from app.secrets.base import CompanyCredentials
from app.secrets.certificate import SigningMaterial, load_signing_material

logger = get_logger(__name__)

DS_NS = "http://www.w3.org/2000/09/xmldsig#"
EXT_NS = "urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2"
C14N_INCLUSIVE = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
ID_FIRMA = "SignatureSP"  # el que referencia el `cac:Signature` de app.sunat.ubl_builder


@runtime_checkable
class XmlSigner(Protocol):
    """Contrato de firma de comprobantes electrónicos."""

    def sign(self, *, xml: bytes, credentials: CompanyCredentials) -> bytes: ...


def _localname(elemento: etree._Element) -> str:
    try:
        return etree.QName(elemento).localname
    except (ValueError, TypeError):
        return ""


def _extension_content(root: etree._Element) -> etree._Element | None:
    """El `ext:ExtensionContent` del documento, si ya existe."""
    return next(
        (elemento for elemento in root.iter() if _localname(elemento) == "ExtensionContent"),
        None,
    )


def _declarar_namespace_ext(root: etree._Element) -> etree._Element:
    """Declara `xmlns:ext` en la raíz (como los XML reales de SUNAT).

    Es imprescindible para que la firma siga siendo válida: la firma se calcula con
    el `ds:Signature` colgando de la raíz y luego se mueve al `ExtensionContent`.
    Con C14N *inclusivo* los namespaces en alcance forman parte de lo que se firma,
    así que si el `ext:` apareciera solo al recolocar la firma, el `SignedInfo`
    canonicalizado cambiaría y la firma ya no verificaría. Declarándolo en la raíz,
    el conjunto de namespaces en alcance es el mismo antes y después del traslado.
    """
    if root.nsmap.get("ext") == EXT_NS:
        return root

    reconstruida = etree.Element(
        root.tag, nsmap={**{k: v for k, v in root.nsmap.items() if k}, "ext": EXT_NS}
    )
    reconstruida.text = root.text
    for clave, valor in root.attrib.items():
        reconstruida.set(clave, valor)
    for hijo in list(root):
        reconstruida.append(hijo)
    return reconstruida


def _preparar_extension_content(root: etree._Element) -> None:
    """Deja listo el hueco de la firma y evita firmar dos veces."""
    contenido = _extension_content(root)
    if contenido is not None:
        if any(_localname(hijo) == "Signature" for hijo in contenido):
            raise InvalidDocumentError(
                "El XML ya tiene una firma digital: no se firma dos veces"
            )
        return

    # Estructura estándar de UBL 2.1, como primer hijo del documento.
    extensions = etree.Element(f"{{{EXT_NS}}}UBLExtensions")
    extension = etree.SubElement(extensions, f"{{{EXT_NS}}}UBLExtension")
    etree.SubElement(extension, f"{{{EXT_NS}}}ExtensionContent")
    root.insert(0, extensions)


def _firma_de_root(root: etree._Element) -> etree._Element | None:
    """El `ds:Signature` que signxml dejó en la raíz (aún sin recolocar).

    Se filtra por namespace: el comprobante generado lleva además un `cac:Signature`
    (datos del firmante) que también cuelga de la raíz y no debe confundirse.
    """
    for hijo in root:
        if isinstance(hijo.tag, str) and hijo.tag == f"{{{DS_NS}}}Signature":
            return hijo
    return None


class SunatXmlSigner:
    """Firma XMLDSig enveloped con el certificado .pfx de la empresa."""

    def sign(self, *, xml: bytes, credentials: CompanyCredentials) -> bytes:
        """Devuelve el XML firmado (mismo documento, con la firma en las extensiones)."""
        if not xml or not xml.strip():
            raise InvalidDocumentError("El XML está vacío")

        try:
            root = etree.fromstring(xml)
        except etree.XMLSyntaxError as exc:
            raise InvalidDocumentError(
                "El archivo no es un XML válido", details={"detalle": str(exc)[:200]}
            ) from exc

        material = load_signing_material(credentials)
        root = _declarar_namespace_ext(root)
        _preparar_extension_content(root)

        try:
            firmado = self._firmar(root, material)
        except ImportError as exc:  # pragma: no cover - depende del entorno
            raise ConfigError(
                "Falta la librería de firma digital (signxml) en el servidor"
            ) from exc

        firma = _firma_de_root(firmado)
        if firma is None:
            raise ConfigError("No se pudo generar la firma del comprobante")

        # `signxml` puede devolver un árbol nuevo: la firma ya calculada se recoloca
        # en el `ExtensionContent` del árbol firmado (el digest no cambia porque la
        # transformación enveloped descarta el elemento firma esté donde esté).
        destino = _extension_content(firmado)
        if destino is None:  # pragma: no cover - defensivo
            raise ConfigError("El XML firmado perdió las extensiones UBL")
        # `cac:Signature/.../URI` del XML generado apunta a «#SignatureSP». El atributo
        # Id no entra en el SignedInfo, así que ponerlo después no invalida la firma.
        if firma.get("Id") is None:
            firma.set("Id", ID_FIRMA)
        destino.append(firma)

        return etree.tostring(firmado, xml_declaration=True, encoding="UTF-8")

    @staticmethod
    def _firmar(root: etree._Element, material: SigningMaterial) -> etree._Element:
        from signxml import XMLSigner, methods

        firmador = XMLSigner(
            method=methods.enveloped,
            signature_algorithm="rsa-sha256",
            digest_algorithm="sha256",
            c14n_algorithm=C14N_INCLUSIVE,
        )
        # `cert` espera la cadena: solo enviamos el certificado de la empresa.
        return firmador.sign(root, key=material.key, cert=[material.certificate])


__all__ = ["SunatXmlSigner", "XmlSigner"]
