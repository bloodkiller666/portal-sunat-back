"""Guard de emisión (regla 4) e interfaz de emisión para la fase posterior.

La emisión real (XML + firma + sendBill) queda fuera de alcance. Lo que sí se deja
listo es el candado: cualquier operación de emisión pasa por
`ensure_emission_allowed`, que bloquea el ambiente de producción mientras
`CONFIRM_PROD_EMISSION` no sea `true`.
"""

from __future__ import annotations

from app.core.config import Settings
from app.core.enums import SunatAmbiente
from app.core.errors import ProdEmissionBlockedError
from app.core.logging import get_logger
from app.db.models import Comprobante
from app.secrets.base import SecretsProvider

logger = get_logger(__name__)


def ensure_emission_allowed(settings: Settings, env: SunatAmbiente | None = None) -> None:
    """Bloquea la emisión en producción sin confirmación explícita."""
    ambiente = settings.effective_sunat_env(env)
    if settings.emission_allowed_for(ambiente):
        return
    logger.warning("Emisión bloqueada: SUNAT_ENV=%s sin CONFIRM_PROD_EMISSION", ambiente.value)
    raise ProdEmissionBlockedError(
        details={"sunat_env": ambiente.value, "confirm_prod_emission": False}
    )


class EmissionService:
    """Punto de entrada de la emisión. Aún sin implementar (fase posterior).

    Existe para que el endpoint de emisión herede el guard de producción desde el
    primer día: cualquier implementación futura debe llamar a
    `ensure_emission_allowed` antes de tocar SUNAT.
    """

    def __init__(self, settings: Settings, secrets_provider: SecretsProvider) -> None:
        self._settings = settings
        self._secrets = secrets_provider

    async def emitir(self, comprobante: Comprobante) -> None:
        ensure_emission_allowed(self._settings, comprobante.sunat_ambiente)
        raise NotImplementedError(
            "La emisión de comprobantes (XML, firma digital y sendBill) es de una fase posterior"
        )
