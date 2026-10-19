"""Interfaz de resolución de secretos por empresa (regla 3).

Hoy la única implementación es `EnvSecretsProvider`; la idea es reemplazarla por
Azure Key Vault sin tocar los servicios (ver `app/secrets/keyvault.py`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator

from app.core.redaction import register_secret_values


class CompanyCredentials(BaseModel):
    """Credenciales SUNAT de una empresa. Es el único objeto que ve los secretos."""

    model_config = ConfigDict(extra="forbid")

    ruc: str
    sol_user: str
    sol_password: SecretStr
    certificate_path: Path | None = None
    certificate_password: SecretStr | None = None
    certificate_base64: SecretStr | None = None
    client_id: str | None = None
    client_secret: SecretStr | None = None
    # Credenciales de la API del OSE (Bizlinks `invoker`). El portal anónimo no las usa.
    bizlinks_user: str | None = None
    bizlinks_password: SecretStr | None = None

    @field_validator("ruc")
    @classmethod
    def _validar_ruc(cls, value: str) -> str:
        value = value.strip()
        if len(value) != 11 or not value.isdigit():
            raise ValueError("El RUC debe tener 11 dígitos")
        return value

    @field_validator("sol_user")
    @classmethod
    def _validar_usuario(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("El usuario SOL no puede estar vacío")
        return value

    @property
    def sunat_username(self) -> str:
        """SUNAT exige Username = RUC + usuario SOL secundario."""
        return f"{self.ruc}{self.sol_user}"

    @property
    def has_certificate(self) -> bool:
        return self.certificate_path is not None or self.certificate_base64 is not None

    @property
    def has_api_credentials(self) -> bool:
        """Credenciales para la API REST con OAuth2 (fase posterior)."""
        return bool(self.client_id and self.client_secret)

    @property
    def has_bizlinks(self) -> bool:
        """Credenciales de la API del OSE (no hacen falta para el portal anónimo)."""
        return bool(self.bizlinks_user and self.bizlinks_password)

    def register_for_redaction(self) -> None:
        """Registra los valores sensibles para que queden fuera de los logs (regla 2)."""
        register_secret_values(
            self.sol_password,
            self.certificate_password,
            self.client_secret,
            self.bizlinks_password,
        )


@runtime_checkable
class SecretsProvider(Protocol):
    """Fuente de credenciales por RUC."""

    def get_company_credentials(self, ruc: str) -> CompanyCredentials:
        """Devuelve las credenciales de la empresa o lanza CredentialsNotConfiguredError."""
        ...

    def list_configured_rucs(self) -> list[str]:
        """RUCs con credenciales disponibles en este entorno (diagnóstico interno)."""
        ...
