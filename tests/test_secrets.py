from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from app.core.config import Settings
from app.core.errors import CredentialsNotConfiguredError
from app.core.redaction import clear_registered_secrets, redact
from app.secrets.base import CompanyCredentials
from app.secrets.certificate import inspect_certificate
from app.secrets.env import EnvSecretsProvider
from app.secrets.factory import build_secrets_provider
from app.secrets.keyvault import AzureKeyVaultSecretsProvider

RUC = "20123456789"
CLAVE_SOL = "clave-sol-de-pruebas"


@pytest.fixture
def entorno() -> dict[str, str]:
    return {
        f"SUNAT_CRED_{RUC}_SOL_USER": "MODDATOS",
        f"SUNAT_CRED_{RUC}_SOL_PASSWORD": CLAVE_SOL,
        f"SUNAT_CRED_{RUC}_CLIENT_ID": "client-id-demo",
        f"SUNAT_CRED_{RUC}_CLIENT_SECRET": "client-secret-demo",
    }


def test_resuelve_credenciales_por_ruc(entorno: dict[str, str]) -> None:
    credentials = EnvSecretsProvider(entorno).get_company_credentials(RUC)
    assert credentials.ruc == RUC
    assert credentials.sunat_username == f"{RUC}MODDATOS"
    assert credentials.sol_password.get_secret_value() == CLAVE_SOL
    assert credentials.has_api_credentials is True
    assert credentials.has_certificate is False


def test_errores_no_incluyen_valores(entorno: dict[str, str]) -> None:
    provider = EnvSecretsProvider(entorno)
    with pytest.raises(CredentialsNotConfiguredError) as error:
        provider.get_company_credentials("20111111111")
    mensaje = str(error.value)
    assert "SUNAT_CRED_20111111111_SOL_USER" in mensaje
    assert CLAVE_SOL not in mensaje


def test_ruc_invalido() -> None:
    with pytest.raises(CredentialsNotConfiguredError):
        EnvSecretsProvider({}).get_company_credentials("123")


def test_clave_no_aparece_en_el_repr(entorno: dict[str, str]) -> None:
    credentials = EnvSecretsProvider(entorno).get_company_credentials(RUC)
    assert CLAVE_SOL not in repr(credentials)
    assert CLAVE_SOL not in str(credentials.model_dump())


def test_registra_los_secretos_para_la_redaccion(entorno: dict[str, str]) -> None:
    clear_registered_secrets()
    try:
        EnvSecretsProvider(entorno).get_company_credentials(RUC)
        assert CLAVE_SOL not in redact(f"usando clave {CLAVE_SOL} contra SUNAT")
        assert "client-secret-demo" not in redact("client_secret=client-secret-demo")
    finally:
        clear_registered_secrets()


def test_lista_rucs_configurados(entorno: dict[str, str]) -> None:
    assert EnvSecretsProvider(entorno).list_configured_rucs() == [RUC]
    assert EnvSecretsProvider({}).list_configured_rucs() == []


def test_fabrica_selecciona_el_proveedor() -> None:
    por_entorno = build_secrets_provider(Settings(_env_file=None))
    assert isinstance(por_entorno, EnvSecretsProvider)

    por_keyvault = build_secrets_provider(
        Settings(_env_file=None, secrets_provider="azure_keyvault", azure_keyvault_url="https://kv")
    )
    assert isinstance(por_keyvault, AzureKeyVaultSecretsProvider)
    with pytest.raises(NotImplementedError):
        por_keyvault.get_company_credentials(RUC)


# --------------------------------------------------------------------------- #
# Certificado
# --------------------------------------------------------------------------- #
def _generar_pfx(destino: Path, password: str) -> None:
    clave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nombre = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "20123456789 ACME SAC")])
    ahora = datetime.now(tz=UTC)
    certificado = (
        x509.CertificateBuilder()
        .subject_name(nombre)
        .issuer_name(nombre)
        .public_key(clave.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(ahora - timedelta(days=1))
        .not_valid_after(ahora + timedelta(days=365))
        .sign(clave, hashes.SHA256())
    )
    destino.write_bytes(
        pkcs12.serialize_key_and_certificates(
            b"certificado",
            clave,
            certificado,
            None,
            serialization.BestAvailableEncryption(password.encode("utf-8")),
        )
    )


def _credenciales(**overrides: object) -> CompanyCredentials:
    base: dict[str, object] = {"ruc": RUC, "sol_user": "MODDATOS", "sol_password": CLAVE_SOL}
    base.update(overrides)
    return CompanyCredentials(**base)


def test_sin_certificado_configurado() -> None:
    info = inspect_certificate(_credenciales())
    assert info.configurado is False
    assert info.abre_con_clave is False
    assert info.vigente is False


def test_certificado_inexistente_se_reporta_sin_explotar(tmp_path: Path) -> None:
    info = inspect_certificate(_credenciales(certificate_path=tmp_path / "no-existe.pfx"))
    assert info.configurado is True
    assert info.abre_con_clave is False
    assert "no existe" in (info.error or "")


def test_certificado_valido_se_abre_y_reporta_vigencia(tmp_path: Path) -> None:
    ruta = tmp_path / "20123456789.pfx"
    _generar_pfx(ruta, "clave-del-certificado")
    info = inspect_certificate(
        _credenciales(certificate_path=ruta, certificate_password="clave-del-certificado")
    )
    assert info.configurado is True
    assert info.abre_con_clave is True
    assert info.origen == "archivo"
    assert "ACME SAC" in (info.sujeto or "")
    assert info.vigente is True
    assert info.dias_para_vencer is not None and info.dias_para_vencer > 300


def test_certificado_con_clave_equivocada(tmp_path: Path) -> None:
    ruta = tmp_path / "20123456789.pfx"
    _generar_pfx(ruta, "clave-correcta")
    info = inspect_certificate(_credenciales(certificate_path=ruta, certificate_password="otra"))
    assert info.configurado is True
    assert info.abre_con_clave is False
    assert "clave" in (info.error or "")


def test_certificado_en_base64_invalido() -> None:
    info = inspect_certificate(_credenciales(certificate_base64="no-es-base64!!"))
    assert info.configurado is True
    assert info.abre_con_clave is False
