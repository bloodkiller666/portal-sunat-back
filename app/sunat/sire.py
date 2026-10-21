"""Cliente de la API REST de SIRE (RVIE, registro de ventas).

Flujo asíncrono por tickets del manual v31: pedir la propuesta (§5.18) → consultar
el estado del ticket (§5.16) → descargar el ZIP generado (§5.17). Es de SOLO
LECTURA: nunca acepta, reemplaza ni registra nada en SUNAT.

El cliente es síncrono (httpx) igual que `SunatConsultClient`; el servicio lo
ejecuta con `run_blocking`. El token (1 h de vida) se reutiliza entre llamadas.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import Settings
from app.core.errors import (
    CredentialsNotConfiguredError,
    SunatAuthError,
    SunatError,
    SunatFaultError,
    SunatUnavailableError,
)
from app.core.redaction import redact, register_secret_values
from app.secrets.base import CompanyCredentials
from app.sunat.endpoints import (
    SIRE_ARCHIVO_REPORTE_URL,
    SIRE_EXPORTAR_PROPUESTA_RCE_URL,
    SIRE_EXPORTAR_PROPUESTA_URL,
    SIRE_RCE_COD_LIBRO,
    SIRE_RVIE_COD_LIBRO,
    SIRE_SCOPE,
    SIRE_TICKETS_URL,
    SIRE_TOKEN_URL,
)

# Anexo III del manual: códigos de estado del ticket.
ESTADO_TERMINADO = "06"
ESTADOS_EN_PROCESO = frozenset({"01", "02", "05"})
ESTADO_CON_ERRORES = "03"

# Tope del ZIP descargado: una propuesta de ventas enorme sigue siendo de MB.
TAMANO_MAXIMO_ZIP = 50 * 1024 * 1024
# Error 422 «1070» (ventas) o «1518» (compras): el periodo no tiene comprobantes (no es un fallo).
CODIGOS_SIN_COMPROBANTES = frozenset({"1070", "1518"})

LIBRO_VENTAS = "rvie"
LIBRO_COMPRAS = "rce"
_COD_LIBRO = {LIBRO_VENTAS: SIRE_RVIE_COD_LIBRO, LIBRO_COMPRAS: SIRE_RCE_COD_LIBRO}

_MARGEN_TOKEN_SEGUNDOS = 60


class SirePeriodoSinDatosError(SunatError):
    status_code = 404
    code = "sire_periodo_sin_datos"
    default_message = "SUNAT no tiene comprobantes en la propuesta de ese periodo"


@dataclass(frozen=True)
class ArchivoTicket:
    nombre: str
    tipo: str | None


@dataclass(frozen=True)
class EstadoTicket:
    numero: str
    periodo: str
    codigo_estado: str
    descripcion: str
    proceso: str | None
    archivos: tuple[ArchivoTicket, ...] = ()

    @property
    def terminado(self) -> bool:
        return self.codigo_estado == ESTADO_TERMINADO

    @property
    def en_proceso(self) -> bool:
        return self.codigo_estado in ESTADOS_EN_PROCESO

    @property
    def con_errores(self) -> bool:
        return self.codigo_estado == ESTADO_CON_ERRORES


@dataclass
class SireClient:
    credentials: CompanyCredentials
    settings: Settings
    http: httpx.Client | None = None
    _token: str | None = field(default=None, init=False, repr=False)
    _token_expira: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        creds = self.credentials
        if not creds.client_id or creds.client_secret is None:
            raise CredentialsNotConfiguredError(
                "La empresa no tiene las credenciales de API SUNAT (client_id/client_secret)"
            )
        register_secret_values(creds.client_secret, creds.sol_password)
        if self.http is None:
            self.http = httpx.Client(
                timeout=httpx.Timeout(float(self.settings.sunat_timeout_seconds)),
                verify=self.settings.sunat_verify_ssl,
            )

    # ------------------------------------------------------------------ #
    # Operaciones del manual
    # ------------------------------------------------------------------ #
    def solicitar_propuesta(self, periodo: str, libro: str = LIBRO_VENTAS) -> str:
        """Pide la propuesta del periodo (AAAAMM) y devuelve el N° de ticket.

        Ventas: §5.18 del manual RVIE. Compras: §5.34 del manual RCE (exige `codOrigenEnvio`).
        """
        if libro == LIBRO_COMPRAS:
            respuesta = self._get(
                SIRE_EXPORTAR_PROPUESTA_RCE_URL.format(periodo=periodo),
                {"codTipoArchivo": 0, "codOrigenEnvio": 2},
            )
        else:
            respuesta = self._get(
                SIRE_EXPORTAR_PROPUESTA_URL.format(periodo=periodo), {"codTipoArchivo": 0}
            )
        cuerpo = _json(respuesta)
        ticket = cuerpo.get("numTicket") if isinstance(cuerpo, dict) else None
        if not ticket:
            raise SunatFaultError("SUNAT no devolvió el número de ticket")
        return str(ticket)

    def consultar_ticket(
        self, ticket: str, periodo: str, libro: str = LIBRO_VENTAS
    ) -> EstadoTicket:
        """§5.16 (ventas) / §5.31 (compras): estado del ticket en el periodo indicado."""
        respuesta = self._get(
            SIRE_TICKETS_URL,
            {
                "perIni": periodo,
                "perFin": periodo,
                "page": 1,
                "perPage": 20,
                "numTicket": ticket,
                "codLibro": _COD_LIBRO[libro],
                "codOrigenEnvio": 2,
            },
        )
        cuerpo = _json(respuesta)
        registros = cuerpo.get("registros") if isinstance(cuerpo, dict) else None
        for registro in registros or []:
            if str(registro.get("numTicket")) != ticket:
                continue
            detalle = registro.get("detalleTicket") or {}
            archivos = tuple(
                ArchivoTicket(
                    nombre=str(archivo.get("nomArchivoReporte") or ""),
                    tipo=archivo.get("codTipoAchivoReporte")
                    or archivo.get("codTipoArchivoReporte"),
                )
                for archivo in (registro.get("archivoReporte") or [])
                if archivo.get("nomArchivoReporte")
            )
            return EstadoTicket(
                numero=ticket,
                periodo=str(registro.get("perTributario") or periodo),
                codigo_estado=str(
                    detalle.get("codEstadoEnvio") or registro.get("codEstadoProceso") or ""
                ),
                descripcion=str(
                    detalle.get("desEstadoEnvio") or registro.get("desEstadoProceso") or ""
                ),
                proceso=str(registro["codProceso"]) if registro.get("codProceso") else None,
                archivos=archivos,
            )
        raise SunatFaultError(
            "SUNAT no reconoce el ticket indicado para ese periodo", details={"ticket": ticket}
        )

    def descargar_archivo(
        self, estado: EstadoTicket, archivo: ArchivoTicket, libro: str = LIBRO_VENTAS
    ) -> bytes:
        """§5.17 (ventas) / §5.32 (compras): descarga el ZIP del ticket terminado."""
        params = {
            "nomArchivoReporte": archivo.nombre,
            "codTipoArchivoReporte": archivo.tipo or "null",
            "perTributario": estado.periodo,
            "codProceso": estado.proceso or "",
            "numTicket": estado.numero,
            "codLibro": _COD_LIBRO[libro],
        }
        respuesta = self._get(SIRE_ARCHIVO_REPORTE_URL, params, accept="*/*")
        if len(respuesta.content) > TAMANO_MAXIMO_ZIP:
            raise SunatFaultError(
                "El archivo de SUNAT supera el tamaño máximo permitido",
                details={"maximo_bytes": TAMANO_MAXIMO_ZIP},
            )
        return respuesta.content

    # ------------------------------------------------------------------ #
    # Infraestructura
    # ------------------------------------------------------------------ #
    def _obtener_token(self) -> str:
        if self._token and time.monotonic() < self._token_expira:
            return self._token
        creds = self.credentials
        assert creds.client_id and creds.client_secret and self.http
        try:
            respuesta = self.http.post(
                SIRE_TOKEN_URL.format(client_id=creds.client_id),
                data={
                    "grant_type": "password",
                    "scope": SIRE_SCOPE,
                    "client_id": creds.client_id,
                    "client_secret": creds.client_secret.get_secret_value(),
                    "username": creds.sunat_username,
                    "password": creds.sol_password.get_secret_value(),
                },
            )
        except httpx.HTTPError as exc:
            raise SunatUnavailableError(
                "No se pudo contactar el servicio de seguridad de SUNAT"
            ) from exc
        if respuesta.status_code in (400, 401, 403):
            raise SunatAuthError(
                "SUNAT rechazó las credenciales de API o la clave SOL de la empresa",
                details={"http_status": respuesta.status_code},
            )
        if respuesta.status_code >= 500:
            raise SunatUnavailableError(details={"http_status": respuesta.status_code})
        cuerpo = _json(respuesta)
        token = cuerpo.get("access_token") if isinstance(cuerpo, dict) else None
        if respuesta.status_code != 200 or not token:
            raise SunatFaultError(
                "SUNAT no entregó el token de acceso",
                details={"http_status": respuesta.status_code},
            )
        register_secret_values(token)
        self._token = str(token)
        vida = int(cuerpo.get("expires_in") or 3600)
        self._token_expira = time.monotonic() + max(vida - _MARGEN_TOKEN_SEGUNDOS, 0)
        return self._token

    def _get(
        self, url: str, params: dict[str, Any], *, accept: str = "application/json"
    ) -> httpx.Response:
        assert self.http
        cabeceras = {"Authorization": f"Bearer {self._obtener_token()}", "Accept": accept}
        try:
            respuesta = self.http.get(url, params=params, headers=cabeceras)
        except httpx.HTTPError as exc:
            raise SunatUnavailableError("No se pudo contactar la API SIRE de SUNAT") from exc
        if respuesta.status_code in (401, 403):
            raise SunatAuthError(
                "SUNAT denegó el acceso a SIRE: la empresa podría no estar acogida o "
                "el usuario SOL no tiene el perfil requerido",
                details={"http_status": respuesta.status_code},
            )
        if respuesta.status_code >= 500:
            raise SunatUnavailableError(details={"http_status": respuesta.status_code})
        if respuesta.status_code == 422:
            codigos = _codigos_422(respuesta)
            if CODIGOS_SIN_COMPROBANTES & set(codigos):
                raise SirePeriodoSinDatosError(details={"codigos": codigos})
            raise SunatFaultError(
                f"SUNAT rechazó la solicitud SIRE (código {', '.join(codigos) or 's/c'})",
                details={"codigos": codigos, "detalle": redact(respuesta.text[:300])},
            )
        if respuesta.status_code != 200:
            raise SunatFaultError(
                "Respuesta inesperada de SIRE", details={"http_status": respuesta.status_code}
            )
        return respuesta


def _json(respuesta: httpx.Response) -> Any:
    try:
        return respuesta.json()
    except ValueError:
        return None


def _codigos_422(respuesta: httpx.Response) -> list[str]:
    cuerpo = _json(respuesta)
    errores = cuerpo.get("errors") if isinstance(cuerpo, dict) else None
    return [str(error.get("cod")) for error in errores or [] if isinstance(error, dict)]
