from __future__ import annotations

import logging

from lxml import etree

from app.core.redaction import (
    REDACTED,
    RedactionFilter,
    clear_registered_secrets,
    redact,
    register_secret_values,
)
from app.sunat.xmlutils import sanitize_xml

CLAVE = "clave-sol-super-secreta"

ENVOLTURA_WS_SECURITY = """<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:ns="http://service.sunat.gob.pe"
                  xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
  <soapenv:Header>
    <wsse:Security>
      <wsse:UsernameToken>
        <wsse:Username>20123456789MODDATOS</wsse:Username>
        <wsse:Password Type="PasswordText">clave-sol-super-secreta</wsse:Password>
      </wsse:UsernameToken>
    </wsse:Security>
  </soapenv:Header>
  <soapenv:Body><ns:getStatus/></soapenv:Body>
</soapenv:Envelope>
"""


def test_enmascara_el_header_ws_security() -> None:
    resultado = redact(ENVOLTURA_WS_SECURITY)
    assert CLAVE not in resultado
    assert REDACTED in resultado
    assert "<wsse:Security>" not in resultado


def test_enmascara_pares_clave_valor() -> None:
    assert CLAVE not in redact(f"configurando clave={CLAVE} para el RUC")
    assert CLAVE not in redact(f'"clave_sol": "{CLAVE}",')
    assert "abc123" not in redact("Authorization: Bearer abc123xyz")
    assert "token-xyz" not in redact("client_secret=token-xyz")


def test_enmascara_claves_privadas_y_base64() -> None:
    pem = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq\n-----END PRIVATE KEY-----"
    assert "MIIEvQIBADANBgkq" not in redact(f"certificado:\n{pem}")
    assert "SEVMTE8=" not in redact("data:application/x-pkcs12;base64,SEVMTE8=")


def test_valores_registrados_se_enmascaran_aunque_no_tengan_patron() -> None:
    clear_registered_secrets()
    try:
        register_secret_values(CLAVE)
        mensaje = f"SUNAT rechazó la clave {CLAVE} del emisor"
        assert CLAVE not in redact(mensaje)
    finally:
        clear_registered_secrets()


def test_el_filtro_limpia_el_registro_antes_de_emitirlo() -> None:
    filtro = RedactionFilter([CLAVE])
    registro = logging.LogRecord(
        name="prueba",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="intentando con la clave %s",
        args=(CLAVE,),
        exc_info=None,
    )
    assert filtro.filter(registro) is True
    assert CLAVE not in registro.getMessage()


def test_sanitize_xml_elimina_la_seguridad_de_un_elemento_lxml() -> None:
    elemento = etree.fromstring(ENVOLTURA_WS_SECURITY.encode("utf-8"))
    saneado = sanitize_xml(elemento)
    assert saneado is not None
    assert CLAVE not in saneado
    assert "Password" not in saneado
    assert "getStatus" in saneado


def test_sanitize_xml_acepta_bytes_y_texto() -> None:
    desde_bytes = sanitize_xml(ENVOLTURA_WS_SECURITY.encode("utf-8"))
    assert desde_bytes is not None
    assert "Security" not in desde_bytes
    assert CLAVE not in desde_bytes


def test_sanitize_xml_no_falla_con_contenido_invalido() -> None:
    # Aunque no sea XML, el saneamiento no puede romper la operación.
    resultado = sanitize_xml("esto no es xml con password=secreto")
    assert resultado is not None
    assert "secreto" not in resultado
    assert sanitize_xml(None) is None


def test_sanitize_xml_trunca_tramas_inmensas() -> None:
    grande = "<a>" + ("x" * 200_000) + "</a>"
    saneado = sanitize_xml(grande)
    assert saneado is not None
    assert len(saneado) < 200_000
    assert "truncada" in saneado
