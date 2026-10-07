from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.enums import SunatAmbiente
from app.core.errors import ProdEmissionBlockedError
from app.secrets.env import EnvSecretsProvider
from app.services.emission import EmissionService, ensure_emission_allowed
from tests.fakes import construir_comprobante, construir_empresa


def test_beta_permite_emitir() -> None:
    ensure_emission_allowed(Settings(_env_file=None, sunat_env="beta"))


def test_prod_bloquea_sin_confirmacion() -> None:
    settings = Settings(_env_file=None, sunat_env="prod", confirm_prod_emission=False)
    with pytest.raises(ProdEmissionBlockedError) as error:
        ensure_emission_allowed(settings)
    assert error.value.details["confirm_prod_emission"] is False
    assert error.value.status_code == 403
    assert "CONFIRM_PROD_EMISSION" in error.value.message


def test_prod_permite_emitir_con_confirmacion() -> None:
    ensure_emission_allowed(Settings(_env_file=None, sunat_env="prod", confirm_prod_emission=True))


def test_el_guard_respeta_el_ambiente_de_la_empresa() -> None:
    settings = Settings(_env_file=None, sunat_env="beta")
    # Una empresa marcada como producción sigue bloqueada aunque el global sea beta.
    with pytest.raises(ProdEmissionBlockedError):
        ensure_emission_allowed(settings, SunatAmbiente.prod)


async def test_el_servicio_de_emision_evalua_el_guard_antes_de_implementarse() -> None:
    empresa = construir_empresa()
    comprobante = construir_comprobante(empresa.id, ambiente=SunatAmbiente.prod)
    servicio = EmissionService(Settings(_env_file=None, sunat_env="prod"), EnvSecretsProvider({}))

    with pytest.raises(ProdEmissionBlockedError):
        await servicio.emitir(comprobante)


async def test_el_servicio_de_emision_sigue_sin_estar_implementado() -> None:
    empresa = construir_empresa()
    comprobante = construir_comprobante(empresa.id, ambiente=SunatAmbiente.beta)
    servicio = EmissionService(Settings(_env_file=None, sunat_env="beta"), EnvSecretsProvider({}))

    with pytest.raises(NotImplementedError):
        await servicio.emitir(comprobante)


def test_las_integraciones_pendientes_avisan_que_no_estan_implementadas() -> None:
    from app.integrations.mailer import SmtpMailSender
    from app.integrations.pdf import SunatPdfRenderer
    from app.integrations.sap import SapSoapClient
    from app.integrations.sharepoint import GraphSharePointGateway

    with pytest.raises(NotImplementedError):
        SunatPdfRenderer()
    with pytest.raises(NotImplementedError):
        GraphSharePointGateway(tenant_id="t", client_id="c", client_secret="s")
    with pytest.raises(NotImplementedError):
        SapSoapClient(url="https://sap", usuario="u", password="p")
    with pytest.raises(NotImplementedError):
        SmtpMailSender(host="smtp", puerto=587, usuario="u", password="p")
