"""Configuración tipada de la aplicación (pydantic-settings).

Todos los valores provienen de variables de entorno o del archivo `.env`
(ignorado por git). Ningún secreto vive en el código (regla 1).
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.enums import SunatAmbiente


class Entorno(StrEnum):
    local = "local"
    dev = "dev"
    staging = "staging"
    prod = "prod"


# Campos opcionales: una cadena vacía en el .env equivale a "no configurado".
_CAMPOS_VACIOS_A_NONE = frozenset(
    {
        "sunat_bill_service_url",
        "sunat_consult_service_url",
        "sunat_wsdl_dir",
        "supabase_url",
        "supabase_jwt_secret",
        "supabase_jwt_jwks_url",
        "supabase_jwt_issuer",
        "database_url",
        "azure_keyvault_url",
    }
)


class Settings(BaseSettings):
    """Configuración de la aplicación."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------- Aplicación ----------------
    app_name: str = "Portal de Facturación Electrónica"
    app_version: str = "0.1.0"
    environment: Entorno = Entorno.local
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    docs_enabled: bool = True
    log_level: str = "INFO"
    log_json: bool = False

    # ---------------- SUNAT ----------------
    sunat_env: SunatAmbiente = SunatAmbiente.beta
    confirm_prod_emission: bool = False
    sunat_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    sunat_max_retries: int = Field(default=2, ge=0, le=5)
    sunat_verify_ssl: bool = True
    sunat_bill_service_url: str | None = None
    sunat_consult_service_url: str | None = None
    sunat_wsdl_dir: Path | None = None
    sunat_probe_tipo: str = "01"
    sunat_probe_serie: str = "F001"
    sunat_probe_numero: str = "1"

    # ---------------- Supabase ----------------
    supabase_url: str | None = None
    supabase_jwt_alg: Literal["HS256", "HS384", "HS512", "ES256", "ES384", "RS256"] = "HS256"
    supabase_jwt_secret: SecretStr | None = None
    supabase_jwt_jwks_url: str | None = None
    supabase_jwt_audience: str = "authenticated"
    supabase_jwt_issuer: str | None = None

    # ---------------- Base de datos ----------------
    database_url: SecretStr | None = None
    database_pool_min_size: int = Field(default=1, ge=1, le=20)
    database_pool_max_size: int = Field(default=10, ge=1, le=50)
    database_command_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    database_apply_rls: bool = True

    # ---------------- CORS ----------------
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    cors_allow_credentials: bool = True
    cors_allow_methods: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["GET", "POST", "PATCH", "DELETE", "OPTIONS"]
    )
    cors_allow_headers: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["Authorization", "Content-Type", "X-Request-Id"]
    )

    # ---------------- Secretos ----------------
    secrets_provider: Literal["env", "azure_keyvault"] = "env"
    azure_keyvault_url: str | None = None

    # ------------------------------------------------------------------ #
    # Validaciones
    # ------------------------------------------------------------------ #
    @field_validator(*sorted(_CAMPOS_VACIOS_A_NONE), mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("cors_origins", "cors_allow_methods", "cors_allow_headers", mode="before")
    @classmethod
    def _parse_list(cls, value: Any) -> Any:
        """Acepta CSV (`a,b`) o JSON (`["a","b"]`) en las variables de entorno."""
        if value is None:
            return []
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if raw.startswith("["):
                return [str(item).strip() for item in json.loads(raw) if str(item).strip()]
            return [item.strip() for item in raw.split(",") if item.strip()]
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_level(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _check_coherencia(self) -> Settings:
        if self.environment in (Entorno.staging, Entorno.prod):
            if "*" in self.cors_origins:
                raise ValueError(
                    "CORS_ORIGINS no puede contener '*' cuando ENVIRONMENT es staging o prod"
                )
            if not self.cors_origins:
                raise ValueError("CORS_ORIGINS es obligatorio en staging y prod")
        if self.database_pool_max_size < self.database_pool_min_size:
            raise ValueError("DATABASE_POOL_MAX_SIZE no puede ser menor que DATABASE_POOL_MIN_SIZE")
        return self

    # ------------------------------------------------------------------ #
    # Derivados
    # ------------------------------------------------------------------ #
    @property
    def is_production_like(self) -> bool:
        return self.environment in (Entorno.staging, Entorno.prod)

    @property
    def database_configured(self) -> bool:
        return self.database_url is not None

    @property
    def jwt_secret_value(self) -> str | None:
        return self.supabase_jwt_secret.get_secret_value() if self.supabase_jwt_secret else None

    @property
    def jwks_url(self) -> str | None:
        if self.supabase_jwt_jwks_url:
            return self.supabase_jwt_jwks_url
        if self.supabase_url:
            return f"{self.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
        return None

    @property
    def jwt_issuer(self) -> str | None:
        if self.supabase_jwt_issuer:
            return self.supabase_jwt_issuer
        if self.supabase_url:
            return f"{self.supabase_url.rstrip('/')}/auth/v1"
        return None

    @property
    def jwt_uses_asymmetric_keys(self) -> bool:
        return not self.supabase_jwt_alg.startswith("HS")

    @property
    def auth_configured(self) -> bool:
        if self.jwt_uses_asymmetric_keys:
            return self.jwks_url is not None
        return self.jwt_secret_value is not None

    def effective_sunat_env(self, company_env: SunatAmbiente | None = None) -> SunatAmbiente:
        """El ambiente global manda, salvo que la empresa tenga uno propio definido."""
        return company_env or self.sunat_env

    def emission_allowed_for(self, env: SunatAmbiente | None = None) -> bool:
        """Regla 4: en `prod` la emisión requiere CONFIRM_PROD_EMISSION=true."""
        if self.effective_sunat_env(env) is SunatAmbiente.beta:
            return True
        return self.confirm_prod_emission

    def startup_warnings(self) -> list[str]:
        """Advertencias de arranque que se registran (nunca con valores secretos)."""
        warnings: list[str] = []
        if not self.auth_configured:
            warnings.append(
                "No hay verificación de JWT configurada (SUPABASE_JWT_SECRET o JWKS): "
                "los endpoints protegidos responderán 503."
            )
        if not self.database_configured:
            warnings.append(
                "DATABASE_URL no está definida: los endpoints que usan datos responderán 503."
            )
        if self.sunat_env is SunatAmbiente.prod and not self.confirm_prod_emission:
            warnings.append(
                "SUNAT_ENV=prod con CONFIRM_PROD_EMISSION=false: la emisión queda bloqueada."
            )
        if self.database_apply_rls and self.database_configured:
            warnings.append(
                "RLS activo: la conexión hará `set local role authenticated` con los claims "
                "del usuario en cada request."
            )
        return warnings


def get_settings() -> Settings:
    """Configuración leída del entorno. Se cachea en `app.state` al crear la app."""
    return Settings()
