"""Inspección del certificado digital de una empresa.

Se usa en `/sunat/status` para verificar que el `.pfx` abre con la clave
configurada y para reportar su vigencia. Solo se devuelven metadatos públicos:
nunca la clave privada ni el contenido del archivo.
"""

from __future__ import annotations

import base64
import binascii
from datetime import UTC, date, datetime
from pathlib import Path

from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509 import Certificate
from pydantic import BaseModel, ConfigDict

from app.core.errors import ConfigError
from app.core.logging import get_logger
from app.secrets.base import CompanyCredentials

logger = get_logger(__name__)


class SigningMaterial(BaseModel):
    """Clave privada y certificado de la empresa, solo en memoria.

    Es el material con el que se firma el XML (firma digital) y la comunicación de
    baja. Nunca se serializa ni se registra en logs.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    ruc: str
    key: object
    certificate: Certificate

    @property
    def certificate_pem(self) -> bytes:
        from cryptography.hazmat.primitives import serialization

        return self.certificate.public_bytes(serialization.Encoding.PEM)


class CertificateInfo(BaseModel):
    """Metadatos del certificado (no sensibles)."""

    configurado: bool = False
    abre_con_clave: bool = False
    origen: str | None = None  # "archivo" | "base64" | None
    sujeto: str | None = None
    emisor: str | None = None
    numero_serie: str | None = None
    valido_desde: datetime | None = None
    valido_hasta: datetime | None = None
    dias_para_vencer: int | None = None
    tiene_clave_privada: bool = False
    error: str | None = None

    @property
    def vigente(self) -> bool:
        if not self.valido_hasta:
            return False
        ahora = datetime.now(tz=UTC)
        hasta = self.valido_hasta
        if hasta.tzinfo is None:
            hasta = hasta.replace(tzinfo=UTC)
        return ahora <= hasta


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _load_pkcs12_bytes(credentials: CompanyCredentials) -> tuple[bytes, str] | None:
    if credentials.certificate_base64:
        certificado_b64 = credentials.certificate_base64.get_secret_value()
        try:
            data = base64.b64decode(certificado_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ConfigError("El certificado en base64 de la empresa es inválido") from exc
        return data, "base64"

    if credentials.certificate_path:
        path: Path = credentials.certificate_path
        if not path.exists():
            return None
        return path.read_bytes(), "archivo"
    return None


def load_signing_material(credentials: CompanyCredentials) -> SigningMaterial:
    """Carga la clave privada y el certificado para firmar (XMLDSig / RA).

    Se lee el PKCS#12 de la empresa (archivo o base64) y se comprueba que traiga
    clave privada: un `.cer` suelto no sirve para firmar. Los errores nunca
    incluyen el material del certificado.
    """
    if not credentials.has_certificate:
        raise ConfigError(
            "La empresa no tiene certificado digital configurado "
            "(SUNAT_CRED_<RUC>_CERT_PATH o _CERT_BASE64)"
        )

    loaded = _load_pkcs12_bytes(credentials)
    if loaded is None:
        raise ConfigError("El archivo del certificado no existe en la ruta configurada")
    data, _origen = loaded

    password = (
        credentials.certificate_password.get_secret_value()
        if credentials.certificate_password
        else None
    )
    try:
        key, certificate, _adicionales = pkcs12.load_key_and_certificates(
            data, password.encode("utf-8") if password else None
        )
    except Exception as exc:
        logger.warning(
            "No se pudo abrir el certificado de la empresa (RUC %s): %s",
            credentials.ruc,
            type(exc).__name__,
        )
        raise ConfigError(
            "No se pudo abrir el certificado con la clave configurada"
        ) from exc

    if key is None or certificate is None:
        raise ConfigError(
            "El certificado no contiene clave privada y certificado: no se puede firmar "
            "con un certificado público"
        )

    return SigningMaterial(ruc=credentials.ruc, key=key, certificate=certificate)


def inspect_certificate(credentials: CompanyCredentials) -> CertificateInfo:
    """Abre el certificado y resume su vigencia sin revelar material sensible."""
    if not credentials.has_certificate:
        return CertificateInfo(configurado=False)

    try:
        loaded = _load_pkcs12_bytes(credentials)
    except ConfigError as exc:
        return CertificateInfo(configurado=True, error=exc.message)

    if loaded is None:
        return CertificateInfo(
            configurado=True,
            origen="archivo",
            error="El archivo del certificado no existe en la ruta configurada",
        )

    data, origen = loaded
    password = (
        credentials.certificate_password.get_secret_value()
        if credentials.certificate_password
        else None
    )

    try:
        key, certificate, _adicionales = pkcs12.load_key_and_certificates(
            data, password.encode("utf-8") if password else None
        )
    except Exception as exc:
        # Solo se registra el tipo de error: el detalle puede exponer material del
        # certificado y queda fuera de la respuesta HTTP.
        logger.warning(
            "No se pudo abrir el certificado de la empresa (RUC %s): %s",
            credentials.ruc,
            type(exc).__name__,
        )
        return CertificateInfo(
            configurado=True,
            origen=origen,
            abre_con_clave=False,
            error="No se pudo abrir el certificado con la clave configurada",
        )

    if certificate is None:
        return CertificateInfo(
            configurado=True,
            origen=origen,
            abre_con_clave=False,
            error="El archivo no contiene un certificado X.509",
        )

    valido_hasta = _as_utc(certificate.not_valid_after_utc)
    valido_desde = _as_utc(certificate.not_valid_before_utc)
    dias = None
    if valido_hasta:
        dias = (valido_hasta.date() - date.today()).days

    return CertificateInfo(
        configurado=True,
        abre_con_clave=key is not None,
        origen=origen,
        sujeto=certificate.subject.rfc4514_string(),
        emisor=certificate.issuer.rfc4514_string(),
        numero_serie=format(certificate.serial_number, "x"),
        valido_desde=valido_desde,
        valido_hasta=valido_hasta,
        dias_para_vencer=dias,
        tiene_clave_privada=key is not None,
    )
