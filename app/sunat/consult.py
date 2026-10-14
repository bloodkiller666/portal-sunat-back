"""Cliente SOAP de solo lectura contra SUNAT (regla 5: solo servicios oficiales).

Operaciones implementadas:

* `billConsultService.getStatus(rucComprobante, tipoComprobante, serieComprobante,
  numeroComprobante)` → estado de un comprobante ya emitido. **Solo existe en
  producción.**
* `billConsultService.getStatusCdr(...)` → estado + CDR (ZIP base64) del comprobante.
* `billService.getStatusAR(ruc, tipo, serie, numero)` → sonda de
  disponibilidad/credenciales en beta. (El `getStatus(fileName)` histórico de beta
  hoy consulta tickets de envíos masivos; la operación vigente por documento es
  `getStatusAR`, verificada contra el WSDL real el 2026-10-12.)

Autenticación: WS-Security `UsernameToken` con `Username = RUC + usuario SOL` y
`PasswordText` con la clave SOL (documentado por SUNAT). Las tramas que se
persisten pasan por `sanitize_xml`, que elimina el header de seguridad.
"""

from __future__ import annotations

import time
from typing import Any

import zeep
from zeep.exceptions import Fault, TransportError
from zeep.plugins import Plugin
from zeep.wsse.username import UsernameToken

from app.core.config import Settings
from app.core.enums import SunatAmbiente
from app.core.errors import (
    SunatAuthError,
    SunatError,
    SunatFaultError,
    SunatUnavailableError,
)
from app.core.logging import get_logger
from app.secrets.base import CompanyCredentials
from app.sunat.cdr import parse_cdr_zip
from app.sunat.endpoints import ServicioSunat, service_url
from app.sunat.faults import clasificar_fault
from app.sunat.models import EstadoSunat, ResultadoConsulta, ResultadoDisponibilidad, TramasSOAP
from app.sunat.transport import HttpxSunatTransport
from app.sunat.wsdl import WsdlLoader
from app.sunat.xmlutils import sanitize_xml

logger = get_logger(__name__)

NOMBRE_OPERACION_ESTADO = "getStatus"
NOMBRE_OPERACION_CDR = "getStatusCdr"
NOMBRE_OPERACION_ESTADO_BETA = "getStatusAR"


class _CapturaTramas(Plugin):
    """Guarda la trama enviada (sin credenciales) y la recibida."""

    def __init__(self) -> None:
        self.request_xml: str | None = None
        self.response_xml: str | None = None

    # Las firmas de los plugins cambian entre versiones de zeep: los argumentos
    # extra se aceptan opcionales.
    def egress(self, envelope, http_headers, operation=None, binding_options=None):
        self.request_xml = sanitize_xml(envelope)
        return envelope, http_headers

    def ingress(self, envelope, http_headers, operation=None):
        self.response_xml = sanitize_xml(envelope)
        return envelope, http_headers


class SunatConsultClient:
    """Cliente por empresa. Se construye por petición (dependencia inyectable)."""

    def __init__(
        self,
        credentials: CompanyCredentials,
        settings: Settings,
        *,
        env: SunatAmbiente | None = None,
        servicio: ServicioSunat = ServicioSunat.consult,
        transport: HttpxSunatTransport | None = None,
        wsdl_loader: WsdlLoader | None = None,
    ) -> None:
        self._credentials = credentials
        self._settings = settings
        self._env = settings.effective_sunat_env(env)
        self._servicio = servicio
        self._transport = transport or HttpxSunatTransport(
            timeout=settings.sunat_timeout_seconds,
            verify_ssl=settings.sunat_verify_ssl,
            max_retries=settings.sunat_max_retries,
        )
        self._wsdl_loader = wsdl_loader or WsdlLoader(settings)
        self._plugin = _CapturaTramas()
        self._client: zeep.Client | None = None
        self.last_tramas: TramasSOAP | None = None

    # ---------------------------------------------------------------- #
    # API
    # ---------------------------------------------------------------- #
    @property
    def ambiente(self) -> SunatAmbiente:
        return self._env

    def get_status(
        self,
        *,
        tipo_comprobante: str,
        serie: str,
        numero: str,
        ruc_comprobante: str | None = None,
    ) -> ResultadoConsulta:
        """Consulta el estado de un comprobante ya emitido."""
        parametros = {
            "rucComprobante": ruc_comprobante or self._credentials.ruc,
            "tipoComprobante": tipo_comprobante,
            "serieComprobante": serie,
            "numeroComprobante": numero,
        }
        return self._consultar(NOMBRE_OPERACION_ESTADO, parametros)

    def get_status_cdr(
        self,
        *,
        tipo_comprobante: str,
        serie: str,
        numero: str,
        ruc_comprobante: str | None = None,
    ) -> ResultadoConsulta:
        """Consulta estado + CDR.

        El WSDL real de `billConsultService` declara exactamente
        `rucComprobante`, `tipoComprobante`, `serieComprobante` y
        `numeroComprobante` (entero). **No** declara `montoTotal`: enviarlo provoca
        `got an unexpected keyword argument 'montoTotal'` (confirmado contra la
        operación real de producción el 2026-10-13).
        """
        parametros: dict[str, Any] = {
            "rucComprobante": ruc_comprobante or self._credentials.ruc,
            "tipoComprobante": tipo_comprobante,
            "serieComprobante": serie,
            "numeroComprobante": _numero_comprobante(numero),
        }
        return self._consultar(NOMBRE_OPERACION_CDR, parametros)

    def check_availability(self) -> ResultadoDisponibilidad:
        """Prueba de conexión y credenciales.

        En beta se usa `billService.getStatus` porque las credenciales de beta solo
        existen en beta: la sonda debe validar el ambiente de la empresa, no la URL
        de consulta de CDR (que vive en producción). En producción se usa
        `billConsultService.getStatus`.
        """
        if self._servicio is ServicioSunat.bill or self._env is SunatAmbiente.beta:
            return self._probar_con_bill_service()
        return self._probar_con_consult_service()

    # ---------------------------------------------------------------- #
    # Implementación
    # ---------------------------------------------------------------- #
    def _probar_con_consult_service(self) -> ResultadoDisponibilidad:
        metodo = NOMBRE_OPERACION_ESTADO
        try:
            resultado = self.get_status(
                tipo_comprobante=self._settings.sunat_probe_tipo,
                serie=self._settings.sunat_probe_serie,
                numero=self._settings.sunat_probe_numero,
            )
        except SunatError as exc:
            return self._disponibilidad_desde_error(exc, metodo)

        return ResultadoDisponibilidad(
            autenticado=True,
            metodo=metodo,
            endpoint=resultado.tramas.endpoint,
            codigo=resultado.estado.codigo,
            mensaje=resultado.estado.mensaje,
            http_status=resultado.tramas.http_status,
            duracion_ms=resultado.tramas.duracion_ms,
            request_xml=resultado.tramas.request_xml,
            response_xml=resultado.tramas.response_xml,
        )

    def _probar_con_bill_service(self) -> ResultadoDisponibilidad:
        """Sonda válida en beta: `billService.getStatusAR(ruc, tipo, serie, numero)`.

        El WSDL vigente de beta declara `getStatus(ticket)` para tickets de envíos
        masivos; la consulta por documento es `getStatusAR`, con la misma forma de
        respuesta (`status.statusCode` / `status.statusMessage`).
        """
        metodo = NOMBRE_OPERACION_ESTADO_BETA
        try:
            resultado, tramas = self._invocar(
                metodo,
                {
                    "rucComprobante": self._credentials.ruc,
                    "tipoComprobante": self._settings.sunat_probe_tipo,
                    "serieComprobante": self._settings.sunat_probe_serie,
                    "numeroComprobante": self._settings.sunat_probe_numero,
                },
                servicio=ServicioSunat.bill,
            )
        except SunatError as exc:
            return self._disponibilidad_desde_error(exc, metodo)

        # zeep desenvuelve la respuesta cuando el mensaje de salida tiene una sola
        # parte: el resultado puede ser el objeto `status` directamente (WSDL
        # vigente) o el envoltorio con `status` anidado (variantes del WSDL).
        contenedor = resultado
        anidado = getattr(resultado, "status", None)
        if anidado is not None and not hasattr(resultado, "statusCode"):
            contenedor = anidado
        codigo = getattr(contenedor, "statusCode", None)
        mensaje = getattr(contenedor, "statusMessage", None)
        return ResultadoDisponibilidad(
            autenticado=True,
            metodo=metodo,
            endpoint=tramas.endpoint,
            codigo=str(codigo) if codigo is not None else None,
            mensaje=str(mensaje) if mensaje else None,
            http_status=tramas.http_status,
            duracion_ms=tramas.duracion_ms,
            request_xml=tramas.request_xml,
            response_xml=tramas.response_xml,
        )

    def _disponibilidad_desde_error(
        self, error: SunatError, metodo: str
    ) -> ResultadoDisponibilidad:
        """Traduce un fault a resultado de prueba.

        Criterio: el header de WS-Security se valida antes de la lógica de negocio,
        así que un fault que no sea de autenticación significa que las credenciales
        sí pasaron. Un fallo de red, en cambio, no dice nada sobre las credenciales.
        """
        tramas = self.last_tramas
        if isinstance(error, SunatAuthError):
            autenticado, tipo = False, "sunat_auth_error"
        elif isinstance(error, SunatUnavailableError):
            autenticado, tipo = False, "sunat_unavailable"
        else:
            autenticado = True
            tipo = "sunat_no_disponible" if error.details.get("no_disponible") else "sunat_fault"

        return ResultadoDisponibilidad(
            autenticado=autenticado,
            metodo=metodo,
            endpoint=tramas.endpoint if tramas else None,
            codigo=str(error.details.get("codigo")) if error.details.get("codigo") else None,
            mensaje=error.message,
            http_status=tramas.http_status if tramas else error.details.get("http_status"),
            duracion_ms=tramas.duracion_ms if tramas else None,
            request_xml=tramas.request_xml if tramas else None,
            response_xml=tramas.response_xml if tramas else None,
            error_tipo=tipo,
        )

    def _consultar(self, metodo: str, parametros: dict[str, Any]) -> ResultadoConsulta:
        resultado, tramas = self._invocar(metodo, parametros)
        codigo, mensaje, contenido = _extraer_estado(resultado)

        cdr = None
        error_cdr = None
        if contenido:
            try:
                cdr = parse_cdr_zip(contenido)
            except SunatError as exc:
                error_cdr = exc.message
                logger.warning("CDR recibido no se pudo interpretar: %s", exc.code)

        return ResultadoConsulta(
            estado=EstadoSunat(
                codigo=codigo,
                mensaje=mensaje,
                tiene_cdr=bool(contenido),
            ),
            cdr=cdr,
            error_cdr=error_cdr,
            tramas=tramas,
        )

    def _invocar(
        self,
        metodo: str,
        parametros: dict[str, Any],
        *,
        servicio: ServicioSunat | None = None,
    ) -> tuple[Any, TramasSOAP]:
        servicio_efectivo = servicio or self._servicio
        endpoint = self._endpoint(servicio_efectivo)
        inicio = time.perf_counter()
        error: SunatError | None = None
        resultado: Any = None

        try:
            zeep_service = self._zeep_service(endpoint, servicio_efectivo)
            resultado = getattr(zeep_service, metodo)(**parametros)
        except Fault as fault:
            error = _fault_a_error(fault, http_status=self._transport.last_status_code)
        except TransportError as exc:
            # zeep envuelve aquí tanto respuestas sin contenido como XML inválido.
            estado = getattr(exc, "status_code", None)
            mensaje = (
                f"SUNAT respondió con un error de transporte SOAP (HTTP {estado})"
                if estado
                else "Error de transporte SOAP con SUNAT"
            )
            error = SunatUnavailableError(
                mensaje, details={"detalle": type(exc).__name__, "http_status": estado}
            )
        except TypeError as exc:
            # Un TypeError al enlazar argumentos suele delatar un WSDL distinto al
            # esperado; cualquier otro TypeError es un fallo inesperado de la librería.
            mensaje = str(exc)
            if "argument" in mensaje:
                error = SunatFaultError(
                    f"La operación {metodo} no acepta los parámetros enviados",
                    details={"operacion": metodo, "detalle": mensaje[:300]},
                )
            else:
                error = SunatFaultError(
                    "Error inesperado al preparar o interpretar la trama SOAP",
                    details={"operacion": metodo, "detalle": mensaje[:300]},
                )

        duracion_ms = int((time.perf_counter() - inicio) * 1000)
        respuesta_saneada = self._plugin.response_xml or sanitize_xml(
            self._transport.last_response_body
        )
        tramas = TramasSOAP(
            endpoint=endpoint,
            metodo=metodo,
            request_xml=self._plugin.request_xml,
            response_xml=respuesta_saneada,
            http_status=self._transport.last_status_code,
            duracion_ms=duracion_ms,
        )
        self.last_tramas = tramas

        if error is not None:
            error.details.setdefault("tramas", tramas)
            raise error

        return resultado, tramas

    def _endpoint(self, servicio: ServicioSunat) -> str:
        url = service_url(servicio, env=self._env, settings=self._settings)
        assert url is not None
        return url

    def _zeep_service(self, endpoint: str, servicio: ServicioSunat):
        if self._client is None:
            wsdl = self._wsdl_loader.resolve(servicio, f"{endpoint}?wsdl")
            self._client = zeep.Client(
                wsdl,
                transport=self._transport,
                wsse=UsernameToken(
                    username=self._credentials.sunat_username,
                    password=self._credentials.sol_password.get_secret_value(),
                ),
                plugins=[self._plugin],
            )
        binding = _binding_name(self._client)
        return self._client.create_service(binding, endpoint)


def _binding_name(client: zeep.Client) -> str:
    for service in client.wsdl.services.values():
        for port in service.ports.values():
            return port.binding.name
    raise SunatError("El WSDL de SUNAT no define ningún puerto SOAP")


def _fault_a_error(fault: Fault, *, http_status: int | None = None) -> SunatError:
    clasificacion = clasificar_fault(fault.code, getattr(fault, "message", None) or str(fault))
    if clasificacion.es_autenticacion:
        return SunatAuthError(
            "SUNAT rechazó las credenciales SOL de la empresa",
            details={"codigo": clasificacion.codigo, "mensaje": clasificacion.mensaje},
        )
    if clasificacion.es_no_disponible:
        return SunatFaultError(
            clasificacion.mensaje or "SUNAT aún no tiene el comprobante",
            details={"codigo": clasificacion.codigo, "no_disponible": True},
        )
    # Un 502/503/504 sin código de negocio es SUNAT caído, no un rechazo.
    if http_status in (502, 503, 504) and not clasificacion.codigo:
        return SunatUnavailableError(
            f"SUNAT no está disponible (HTTP {http_status})",
            details={"http_status": http_status, "mensaje": clasificacion.mensaje},
        )
    return SunatFaultError(
        clasificacion.mensaje,
        details={"codigo": clasificacion.codigo},
    )


def _numero_comprobante(numero: str) -> int | str:
    """El WSDL de `getStatusCdr` declara `numeroComprobante` como entero."""
    texto = str(numero).strip()
    return int(texto) if texto.isdigit() else texto


def _extraer_estado(resultado: Any) -> tuple[str | None, str | None, Any | None]:
    """Normaliza la respuesta: `status` (getStatus) o `statusCdr` (getStatusCdr)."""
    contenedores: list[Any] = []
    for atributo in ("statusCdr", "status"):
        contenedor = getattr(resultado, atributo, None)
        if contenedor is not None:
            contenedores.append(contenedor)
    if resultado is not None:
        contenedores.append(resultado)

    for contenedor in contenedores:
        codigo = getattr(contenedor, "statusCode", None)
        mensaje = getattr(contenedor, "statusMessage", None)
        contenido = getattr(contenedor, "content", None)
        if codigo is not None or contenido is not None or mensaje is not None:
            return (
                str(codigo) if codigo is not None else None,
                str(mensaje) if mensaje is not None else None,
                contenido,
            )
    return None, None, None
