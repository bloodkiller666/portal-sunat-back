"""Selecciona la implementación de `SecretsProvider` según la configuración."""

from __future__ import annotations

from app.core.config import Settings
from app.core.logging import get_logger
from app.secrets.base import SecretsProvider
from app.secrets.env import EnvSecretsProvider
from app.secrets.keyvault import AzureKeyVaultSecretsProvider

logger = get_logger(__name__)


def build_secrets_provider(settings: Settings) -> SecretsProvider:
    if settings.secrets_provider == "azure_keyvault":
        if not settings.azure_keyvault_url:
            logger.warning("SECRETS_PROVIDER=azure_keyvault sin AZURE_KEYVAULT_URL definido")
        logger.warning(
            "SECRETS_PROVIDER=azure_keyvault todavía no está implementado: "
            "las credenciales fallarán hasta completar esa integración"
        )
        return AzureKeyVaultSecretsProvider(settings)

    return EnvSecretsProvider()
