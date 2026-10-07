"""URLs oficiales de los servicios web de SUNAT.

Fuente: micrositio de Comprobantes de Pago Electrónicos (cpe.sunat.gob.pe) y
`orientacion.sunat.gob.pe/01-servicios-web-disponibles`.

Servicios web para consultas (no requieren emisión):

* `billConsultService` — estado y CDR de un comprobante ya emitido.
  **Solo está publicado en producción**: no existe endpoint beta, por lo que
  también en `beta` se usa la URL de producción. Es una decisión del proyecto:
  consultar el CDR es una operación de SOLO LECTURA que no emite ni altera
  comprobantes tributarios. Requiere credenciales SOL de producción (las de beta
  no autentican contra producción).
* `billValidService` — validez del XML, público y sin credenciales (fase posterior).
"""

from __future__ import annotations

from enum import StrEnum

from app.core.config import Settings
from app.core.enums import SunatAmbiente
from app.core.errors import SunatConsultNotAvailableError

WSDL_SUFFIX = "?wsdl"


class ServicioSunat(StrEnum):
    bill = "billService"
    consult = "billConsultService"
    valid = "billValidService"


_URLS_OFICIALES: dict[SunatAmbiente, dict[ServicioSunat, str | None]] = {
    SunatAmbiente.beta: {
        # Servicio exclusivo para pruebas (facturas, boletas y notas).
        ServicioSunat.bill: "https://e-beta.sunat.gob.pe/ol-ti-itcpfegem-beta/billService",
        # La consulta de CDR no está publicada en beta, así que se apunta a
        # producción incluso en beta: es de solo lectura y no altera nada.
        ServicioSunat.consult: (
            "https://e-factura.sunat.gob.pe/ol-it-wsconscpegem/billConsultService"
        ),
        # La validación de comprobantes no se usa en esta fase.
        ServicioSunat.valid: None,
    },
    SunatAmbiente.prod: {
        ServicioSunat.bill: "https://e-factura.sunat.gob.pe/ol-ti-itcpfegem/billService",
        ServicioSunat.consult: (
            "https://e-factura.sunat.gob.pe/ol-it-wsconscpegem/billConsultService"
        ),
        ServicioSunat.valid: (
            "https://e-factura.sunat.gob.pe/ol-it-wsconsvalidcpe/billValidService"
        ),
    },
}


def _override(settings: Settings | None, servicio: ServicioSunat) -> str | None:
    if settings is None:
        return None
    if servicio is ServicioSunat.bill:
        return settings.sunat_bill_service_url
    if servicio is ServicioSunat.consult:
        return settings.sunat_consult_service_url
    return None


def service_url(
    servicio: ServicioSunat,
    *,
    env: SunatAmbiente,
    settings: Settings | None = None,
    required: bool = True,
) -> str | None:
    """Devuelve la URL del servicio para el ambiente indicado.

    Un override por variable de entorno tiene prioridad (útil para pruebas o si
    SUNAT publica el servicio de consulta en beta en el futuro).
    """
    override = _override(settings, servicio)
    if override:
        return override.rstrip("/").removesuffix(WSDL_SUFFIX)

    url = _URLS_OFICIALES[env].get(servicio)
    if url is None and required:
        raise SunatConsultNotAvailableError(
            f"El servicio {servicio.value} no está disponible en el ambiente {env.value}"
        )
    return url


def wsdl_url(
    servicio: ServicioSunat,
    *,
    env: SunatAmbiente,
    settings: Settings | None = None,
) -> str:
    """URL del WSDL del servicio (`?wsdl`)."""
    url = service_url(servicio, env=env, settings=settings)
    assert url is not None  # required=True garantiza una URL
    return f"{url}{WSDL_SUFFIX}"
