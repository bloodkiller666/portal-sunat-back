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


# --------------------------------------------------------------------------- #
# SIRE (API REST). Fuente: «Manual del servicio API del Registro de Ventas e
# Información Electrónica» v31 (cpe.sunat.gob.pe), §5.1, §5.2 y §5.18.
# --------------------------------------------------------------------------- #
SIRE_SCOPE = "https://api-sire.sunat.gob.pe"
SIRE_TOKEN_URL = "https://api-seguridad.sunat.gob.pe/v1/clientessol/{client_id}/oauth2/token/"
SIRE_RVIE_COD_LIBRO = "140000"
SIRE_PERIODOS_RVIE_URL = (
    SIRE_SCOPE + "/v1/contribuyente/migeigv/libros/rvierce/padron/web/omisos/"
    + SIRE_RVIE_COD_LIBRO + "/periodos"
)
SIRE_EXPORTAR_PROPUESTA_URL = (
    SIRE_SCOPE + "/v1/contribuyente/migeigv/libros/rvie/propuesta/web/propuesta/"
    "{periodo}/exportapropuesta"
)
_SIRE_MASIVO = "/v1/contribuyente/migeigv/libros/rvierce/gestionprocesosmasivos/web/masivo/"
SIRE_TICKETS_URL = SIRE_SCOPE + _SIRE_MASIVO + "consultaestadotickets"
SIRE_ARCHIVO_REPORTE_URL = SIRE_SCOPE + _SIRE_MASIVO + "archivoreporte"

# --------------------------------------------------------------------------- #
# RCE (registro de compras). Fuente: «Manual de servicios Web Api - SIRE Compras» v28
# (cpe.sunat.gob.pe), §5.34 (descargar propuesta). Los servicios de tickets y descarga de
# archivos (§5.31, §5.32) son los mismos `rvierce` de ventas: solo cambia `codLibro`.
# --------------------------------------------------------------------------- #
SIRE_RCE_COD_LIBRO = "080000"
SIRE_EXPORTAR_PROPUESTA_RCE_URL = (
    SIRE_SCOPE + "/v1/contribuyente/migeigv/libros/rce/propuesta/web/propuesta/"
    "{periodo}/exportacioncomprobantepropuesta"
)
