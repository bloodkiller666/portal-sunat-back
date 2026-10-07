from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.enums import SunatAmbiente
from app.core.errors import SunatConsultNotAvailableError
from app.sunat.endpoints import ServicioSunat, service_url, wsdl_url

# URLs publicadas por SUNAT (orientacion.sunat.gob.pe / cpe.sunat.gob.pe).
BILL_BETA = "https://e-beta.sunat.gob.pe/ol-ti-itcpfegem-beta/billService"
BILL_PROD = "https://e-factura.sunat.gob.pe/ol-ti-itcpfegem/billService"
CONSULT_PROD = "https://e-factura.sunat.gob.pe/ol-it-wsconscpegem/billConsultService"
VALID_PROD = "https://e-factura.sunat.gob.pe/ol-it-wsconsvalidcpe/billValidService"


def test_urls_de_bill_service_por_ambiente() -> None:
    assert service_url(ServicioSunat.bill, env=SunatAmbiente.beta) == BILL_BETA
    assert service_url(ServicioSunat.bill, env=SunatAmbiente.prod) == BILL_PROD


def test_urls_de_consulta_y_validez_en_produccion() -> None:
    assert service_url(ServicioSunat.consult, env=SunatAmbiente.prod) == CONSULT_PROD
    assert service_url(ServicioSunat.valid, env=SunatAmbiente.prod) == VALID_PROD


def test_la_consulta_de_cdr_usa_produccion_tambien_en_beta() -> None:
    """billConsultService no está publicado en beta.

    Decisión del proyecto: la consulta de CDR es de solo lectura y no emite ni
    altera comprobantes, así que se apunta a la URL de producción en ambos
    ambientes. Requiere credenciales SOL de producción.
    """
    assert service_url(ServicioSunat.consult, env=SunatAmbiente.beta) == CONSULT_PROD
    assert wsdl_url(ServicioSunat.consult, env=SunatAmbiente.beta) == f"{CONSULT_PROD}?wsdl"


def test_la_validacion_de_comprobantes_no_existe_en_beta() -> None:
    with pytest.raises(SunatConsultNotAvailableError, match="billValidService"):
        service_url(ServicioSunat.valid, env=SunatAmbiente.beta)


def test_consulta_en_beta_se_puede_forzar_por_override() -> None:
    settings = Settings(
        _env_file=None, sunat_consult_service_url="https://beta.example.com/billConsultService"
    )
    assert (
        service_url(ServicioSunat.consult, env=SunatAmbiente.beta, settings=settings)
        == "https://beta.example.com/billConsultService"
    )


def test_override_ignora_el_sufijo_wsdl() -> None:
    settings = Settings(
        _env_file=None, sunat_bill_service_url="https://local.test/billService?wsdl"
    )
    assert (
        service_url(ServicioSunat.bill, env=SunatAmbiente.prod, settings=settings)
        == "https://local.test/billService"
    )


def test_wsdl_url_agrega_el_sufijo() -> None:
    assert wsdl_url(ServicioSunat.bill, env=SunatAmbiente.beta) == f"{BILL_BETA}?wsdl"
    assert wsdl_url(ServicioSunat.consult, env=SunatAmbiente.prod) == f"{CONSULT_PROD}?wsdl"


def test_servicio_no_requerido_devuelve_none() -> None:
    assert service_url(ServicioSunat.valid, env=SunatAmbiente.beta, required=False) is None
