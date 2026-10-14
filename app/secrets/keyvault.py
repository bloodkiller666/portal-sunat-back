"""Proveedor de secretos con Azure Key Vault (fase posterior, sin implementar).

Cuando se habilite, la convención de nombres se mantiene respecto a
`EnvSecretsProvider` para no cambiar los servicios:

    sunat-cred-{RUC}-sol-user / -sol-password / -cert-password
    sunat-cred-{RUC}-cert-pfx   (el .pfx guardado como secreto base64)

La firma de `SecretsProvider` es la misma, así que el cambio se limita a la
fábrica (`app/secrets/factory.py`).
"""

from __future__ import annotations

from app.core.config import Settings
from app.secrets.base import CompanyCredentials


class AzureKeyVaultSecretsProvider:
    """Implementación pendiente. No debe usarse todavía."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def get_company_credentials(self, ruc: str) -> CompanyCredentials:
        raise NotImplementedError(
            "Azure Key Vault todavía no está implementado. Use SECRETS_PROVIDER=env"
        )

    def list_configured_rucs(self) -> list[str]:
        raise NotImplementedError(
            "Azure Key Vault todavía no está implementado. Use SECRETS_PROVIDER=env"
        )
