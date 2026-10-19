"""Proveedor de secretos por variables de entorno (desarrollo).

Convención (una por empresa):

    SUNAT_CRED_{RUC}_SOL_USER        usuario SOL secundario (sin el RUC)
    SUNAT_CRED_{RUC}_SOL_PASSWORD    clave SOL
    SUNAT_CRED_{RUC}_CERT_PATH       ruta al .pfx (carpeta `secrets/`, ignorada por git)
    SUNAT_CRED_{RUC}_CERT_PASSWORD   clave del .pfx
    SUNAT_CRED_{RUC}_CLIENT_ID       credenciales de la API REST (fase posterior)
    SUNAT_CRED_{RUC}_CLIENT_SECRET
    SUNAT_CRED_{RUC}_BIZLINKS_USER      usuario de la API del OSE (Bizlinks `invoker`)
    SUNAT_CRED_{RUC}_BIZLINKS_PASSWORD  clave de la API del OSE
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from app.core.errors import CredentialsNotConfiguredError
from app.core.logging import get_logger
from app.secrets.base import CompanyCredentials

logger = get_logger(__name__)

PREFIX = "SUNAT_CRED_"
_RUC_RE = re.compile(r"^[0-9]{11}$")
_RUC_EN_CLAVES = re.compile(rf"^{PREFIX}(?P<ruc>\d{{11}})_SOL_USER$")


class EnvSecretsProvider:
    """Lee las credenciales del entorno (o de un mapping inyectado en pruebas)."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ: Mapping[str, str] = dict(os.environ if environ is None else environ)

    # ---------------------------------------------------------------- #
    # SecretsProvider
    # ---------------------------------------------------------------- #
    def get_company_credentials(self, ruc: str) -> CompanyCredentials:
        ruc = (ruc or "").strip()
        if not _RUC_RE.match(ruc):
            raise CredentialsNotConfiguredError(
                "El RUC debe tener 11 dígitos para resolver credenciales"
            )

        sol_user = self._get(ruc, "SOL_USER")
        sol_password = self._get(ruc, "SOL_PASSWORD")
        if not sol_user or not sol_password:
            # Se nombra la variable faltante, nunca su valor.
            raise CredentialsNotConfiguredError(
                "No hay credenciales SUNAT para esta empresa. Configure "
                f"{PREFIX}{ruc}_SOL_USER y {PREFIX}{ruc}_SOL_PASSWORD (o "
                f"{PREFIX}{ruc}_CERT_* según el caso)."
            )

        certificate_path = self._get(ruc, "CERT_PATH")
        certificate_base64 = self._get(ruc, "CERT_BASE64")

        credentials = CompanyCredentials(
            ruc=ruc,
            sol_user=sol_user,
            sol_password=sol_password,
            certificate_path=Path(certificate_path) if certificate_path else None,
            certificate_password=self._get(ruc, "CERT_PASSWORD") or None,
            certificate_base64=certificate_base64 or None,
            client_id=self._get(ruc, "CLIENT_ID") or None,
            client_secret=self._get(ruc, "CLIENT_SECRET") or None,
            bizlinks_user=self._get(ruc, "BIZLINKS_USER") or None,
            bizlinks_password=self._get(ruc, "BIZLINKS_PASSWORD") or None,
        )
        credentials.register_for_redaction()
        logger.debug(
            "Credenciales resueltas para el RUC %s (certificado=%s)",
            ruc,
            "sí" if credentials.has_certificate else "no",
        )
        return credentials

    def list_configured_rucs(self) -> list[str]:
        rucs = {
            match.group("ruc")
            for key in self._environ
            if (match := _RUC_EN_CLAVES.match(key.upper()))
        }
        return sorted(rucs)

    # ---------------------------------------------------------------- #
    # Internos
    # ---------------------------------------------------------------- #
    def _get(self, ruc: str, field: str) -> str | None:
        value = self._environ.get(f"{PREFIX}{ruc}_{field}")
        if value is None:
            return None
        value = value.strip()
        return value or None
