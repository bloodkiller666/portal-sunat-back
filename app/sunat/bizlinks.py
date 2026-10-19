"""Cliente del web service `invoker` de Bizlinks (OSE) para recuperar XML y CDR.

TDM emite a través de Bizlinks: es quien firma, valida y declara, así que conserva el
XML firmado y el CDR de cada comprobante. SUNAT no los entrega (el SIRE solo lista los
datos y `getStatusCdr` responde `0013` para lo que no pasó por su canal), de modo que
esta es la fuente de respaldo para auditoría.

Solo se usa la operación de **consulta** (`ConsultCmd`, manual «Documento de Integración
Técnico Funcional V4.0», §5.2.5). Devuelve, por comprobante, las URLs del XML firmado
(`xmlFileSignUrl`) y de la respuesta de SUNAT/OSE (`xmlFileSunatUrl`, el CDR). Las URLs se
descargan aparte y solo desde los dominios permitidos en la configuración.

Se habla SOAP directo con httpx (sin zeep): el cuerpo es un único parámetro `command`
con XML dentro de CDATA, y la respuesta trae otro XML dentro de `return`.

Pendiente de confirmar con Bizlinks (el manual no lo detalla): el mecanismo exacto de
autenticación (aquí HTTP Basic con usuario y clave, que es lo que el manual describe como
«autenticación por usuario y password» y su tabla de errores 401) y el dominio desde el
que sirven los archivos.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from urllib.parse import urlparse

import httpx
from lxml import etree

from app.core.config import Settings
from app.core.errors import (
    BizlinksAuthError,
    BizlinksError,
    BizlinksNotConfiguredError,
    BizlinksUnavailableError,
)
from app.core.logging import get_logger
from app.secrets.base import CompanyCredentials

logger = get_logger(__name__)

NS_SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
NS_WS = "http://ws.ce.ebiz.com/"

# Tope de un archivo descargado: un XML o CDR pesa unos KB; esto corta respuestas absurdas.
TAMANO_MAXIMO_DESCARGA = 10 * 1024 * 1024

_TIPO_RE = re.compile(r"^[0-9A-Z]{2}$")
_SERIE_RE = re.compile(r"^[A-Z0-9]{4}$")
_CORRELATIVO_RE = re.compile(r"^[0-9]{1,8}$")
_RUC_RE = re.compile(r"^[0-9]{11}$")


@dataclass(frozen=True, slots=True)
class DocumentoBizlinks:
    """Un comprobante tal como lo informa Bizlinks."""

    tipo_documento: str
    id_documento: str  # SERIE-NÚMERO
    status: str | None = None
    status_documento: str | None = None
    status_sunat: str | None = None
    mensaje_sunat: str | None = None
    xml_firmado_url: str | None = None
    xml_sunat_url: str | None = None  # CDR
    pdf_url: str | None = None


@dataclass(frozen=True, slots=True)
class RespaldosBizlinks:
    """Lo que Bizlinks entregó de un comprobante: interfaz común del API y del portal."""

    encontrado: bool
    xml: bytes | None = None  # XML UBL firmado
    cdr: bytes | None = None  # ZIP del CDR (o su XML suelto)
    avisos: tuple[str, ...] = ()


def _localname(elemento: etree._Element) -> str:
    return etree.QName(elemento).localname if isinstance(elemento.tag, str) else ""


def _texto(elemento: etree._Element, nombre: str) -> str | None:
    for hijo in elemento:
        if _localname(hijo) == nombre:
            valor = (hijo.text or "").strip()
            return valor or None
    return None


def construir_consulta(
    *, ruc: str, tipo_documento: str, serie: str, desde: str, hasta: str
) -> str:
    """El `ConsultCmd` que va dentro del parámetro `command` (valores ya validados)."""
    return (
        '<ConsultCmd output=""><parametros/>'
        f'<parameter value="{ruc}" name="idEmisor"/>'
        f'<parameter value="{tipo_documento}" name="tipoDocumento"/>'
        f'<parameter value="{serie}" name="serieGrupoDocumento"/>'
        f'<parameter value="{desde}" name="numeroCorrelativoInicio"/>'
        f'<parameter value="{hasta}" name="numeroCorrelativoFin"/>'
        "</ConsultCmd>"
    )


def construir_sobre(comando: str) -> bytes:
    """Sobre SOAP de `invoke`: el comando viaja en CDATA, como en el manual."""
    if "]]>" in comando:  # imposible con valores validados, pero CDATA no admite el cierre
        raise BizlinksError("El comando de consulta contiene caracteres no permitidos")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<soapenv:Envelope xmlns:soapenv="{NS_SOAP}" xmlns:ws="{NS_WS}">'
        "<soapenv:Header/><soapenv:Body><ws:invoke>"
        f"<command><![CDATA[{comando}]]></command>"
        "</ws:invoke></soapenv:Body></soapenv:Envelope>"
    ).encode()


def parse_respuesta(contenido: bytes) -> list[DocumentoBizlinks]:
    """Extrae los documentos de la respuesta de `invoke` (SOAP → `return` → ebizResponse)."""
    try:
        sobre = etree.fromstring(contenido)
    except etree.XMLSyntaxError as exc:
        raise BizlinksError("Bizlinks devolvió una respuesta que no es XML") from exc

    for elemento in sobre.iter():
        if _localname(elemento) == "Fault":
            mensaje = next(
                ((e.text or "").strip() for e in elemento.iter() if _localname(e) == "faultstring"),
                "",
            )
            raise BizlinksError(
                "Bizlinks respondió con un error SOAP", details={"detalle": mensaje[:300]}
            )

    retorno = next((e for e in sobre.iter() if _localname(e) == "return"), None)
    if retorno is None or not (retorno.text or "").strip():
        raise BizlinksError("La respuesta de Bizlinks no contiene el resultado de la consulta")

    try:
        respuesta = etree.fromstring(retorno.text.strip().encode("utf-8"))
    except etree.XMLSyntaxError as exc:
        raise BizlinksError("El resultado de Bizlinks no es un XML válido") from exc

    comun = next((e for e in respuesta.iter() if _localname(e) == "commonResponse"), None)
    estado = _texto(comun, "status") if comun is not None else None
    if estado is not None and estado.upper() != "OK":
        descripcion = (_texto(comun, "descriptionStatus") or "") if comun is not None else ""
        raise BizlinksError(
            "Bizlinks rechazó la consulta",
            details={"estado": estado, "detalle": descripcion[:300]},
        )

    documentos: list[DocumentoBizlinks] = []
    for documento in respuesta.iter():
        if _localname(documento) != "document":
            continue
        tipo = _texto(documento, "typeDocument")
        identificador = _texto(documento, "idDocument")
        if not tipo or not identificador:
            continue
        documentos.append(
            DocumentoBizlinks(
                tipo_documento=tipo,
                id_documento=identificador,
                status=_texto(documento, "status"),
                status_documento=_texto(documento, "statusDocument"),
                status_sunat=_texto(documento, "statusSunat"),
                mensaje_sunat=_texto(documento, "messageSunat"),
                xml_firmado_url=_texto(documento, "xmlFileSignUrl"),
                xml_sunat_url=_texto(documento, "xmlFileSunatUrl"),
                pdf_url=_texto(documento, "pdfFileUrl"),
            )
        )
    return documentos


def host_permitido(url: str, sufijos: list[str]) -> bool:
    """La URL es de un dominio permitido (el dominio exacto o un subdominio suyo)."""
    anfitrion = (urlparse(url).hostname or "").lower()
    return bool(anfitrion) and any(
        anfitrion == sufijo.lower() or anfitrion.endswith("." + sufijo.lower())
        for sufijo in sufijos
    )


def es_el_documento(
    documento: DocumentoBizlinks, tipo_documento: str, serie: str, correlativo: int
) -> bool:
    """Bizlinks informa `SERIE-NÚMERO`; se compara por serie y número, no por texto."""
    serie_doc, _, numero = documento.id_documento.partition("-")
    return (
        documento.tipo_documento == tipo_documento
        and serie_doc.upper() == serie.upper()
        and numero.isdigit()
        and int(numero) == correlativo
    )


class BizlinksClient:
    """Cliente por empresa (se construye por petición, como `SunatConsultClient`)."""

    canal = "api"

    def __init__(
        self,
        credentials: CompanyCredentials,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        if not credentials.has_bizlinks or not settings.bizlinks_service_url:
            raise BizlinksNotConfiguredError(
                "Falta configurar Bizlinks: BIZLINKS_SERVICE_URL y "
                f"SUNAT_CRED_{credentials.ruc}_BIZLINKS_USER / _BIZLINKS_PASSWORD"
            )
        self._credentials = credentials
        self._settings = settings
        self._url = settings.bizlinks_service_url.rstrip("/").removesuffix("?wsdl")
        self._client = client or httpx.Client(
            timeout=settings.sunat_timeout_seconds, verify=settings.sunat_verify_ssl
        )

    # ---------------------------------------------------------------- #
    # API
    # ---------------------------------------------------------------- #
    def consultar(
        self,
        *,
        tipo_documento: str,
        serie: str,
        correlativo_desde: int,
        correlativo_hasta: int | None = None,
    ) -> list[DocumentoBizlinks]:
        """Consulta un comprobante (o un rango de la misma serie) emitido por la empresa."""
        desde = f"{correlativo_desde:08d}"
        hasta = f"{(correlativo_hasta or correlativo_desde):08d}"
        for valor, patron, nombre in (
            (self._credentials.ruc, _RUC_RE, "RUC"),
            (tipo_documento, _TIPO_RE, "tipo de documento"),
            (serie, _SERIE_RE, "serie"),
            (desde, _CORRELATIVO_RE, "correlativo"),
            (hasta, _CORRELATIVO_RE, "correlativo"),
        ):
            if not patron.match(valor):
                raise BizlinksError(f"El {nombre} no es válido para consultar en Bizlinks")

        comando = construir_consulta(
            ruc=self._credentials.ruc,
            tipo_documento=tipo_documento,
            serie=serie,
            desde=desde,
            hasta=hasta,
        )
        respuesta = self._enviar(construir_sobre(comando))
        return parse_respuesta(respuesta)

    def obtener_respaldos(
        self,
        *,
        tipo_documento: str,
        serie: str,
        correlativo: int,
        fecha_emision: date | None = None,
        monto_total: Decimal | None = None,
        necesita_xml: bool = True,
        necesita_cdr: bool = True,
    ) -> RespaldosBizlinks:
        """Consulta el comprobante y descarga el XML y/o el CDR que se pidan.

        La fecha y el monto no hacen falta aquí (la API consulta por serie y número);
        existen para que el portal anónimo comparta la misma interfaz.
        """
        documentos = self.consultar(
            tipo_documento=tipo_documento, serie=serie, correlativo_desde=correlativo
        )
        documento = next(
            (d for d in documentos if es_el_documento(d, tipo_documento, serie, correlativo)),
            None,
        )
        if documento is None:
            return RespaldosBizlinks(encontrado=False)

        avisos: list[str] = []
        xml = cdr = None
        if necesita_xml:
            if documento.xml_firmado_url:
                xml = self.descargar(documento.xml_firmado_url)
            else:
                avisos.append("Bizlinks no informó la URL del XML firmado")
        if necesita_cdr:
            if documento.xml_sunat_url:
                cdr = self.descargar(documento.xml_sunat_url)
            else:
                avisos.append("Bizlinks aún no informa el CDR de este comprobante")
        return RespaldosBizlinks(encontrado=True, xml=xml, cdr=cdr, avisos=tuple(avisos))

    def descargar(self, url: str) -> bytes:
        """Descarga un archivo desde una URL de la respuesta (solo dominios permitidos).

        No se envían credenciales: las URLs de archivos son autosuficientes y mandar el
        usuario y la clave a una dirección que viene en una respuesta sería una fuga.
        """
        if urlparse(url).scheme not in ("http", "https") or not host_permitido(
            url, self._settings.bizlinks_download_hosts
        ):
            raise BizlinksError(
                "La URL del archivo no pertenece a un dominio permitido de Bizlinks",
                details={"anfitrion": urlparse(url).hostname},
            )
        try:
            with self._client.stream("GET", url) as respuesta:
                if respuesta.status_code == 404:
                    raise BizlinksError("Bizlinks ya no tiene el archivo solicitado")
                if respuesta.status_code >= 400:
                    raise BizlinksUnavailableError(
                        f"Bizlinks respondió HTTP {respuesta.status_code} al descargar",
                        details={"http_status": respuesta.status_code},
                    )
                partes: list[bytes] = []
                total = 0
                for fragmento in respuesta.iter_bytes():
                    total += len(fragmento)
                    if total > TAMANO_MAXIMO_DESCARGA:
                        raise BizlinksError("El archivo de Bizlinks supera el tamaño permitido")
                    partes.append(fragmento)
        except httpx.TransportError as exc:
            raise BizlinksUnavailableError(
                "No se pudo descargar el archivo de Bizlinks",
                details={"error": type(exc).__name__},
            ) from exc
        return b"".join(partes)

    # ---------------------------------------------------------------- #
    # Internos
    # ---------------------------------------------------------------- #
    def _enviar(self, sobre: bytes) -> bytes:
        usuario = self._credentials.bizlinks_user or ""
        clave = self._credentials.bizlinks_password
        assert clave is not None  # garantizado por has_bizlinks
        intentos = self._settings.sunat_max_retries + 1
        ultimo: Exception | None = None

        for intento in range(intentos):
            try:
                respuesta = self._client.post(
                    self._url,
                    content=sobre,
                    headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": ""},
                    auth=(usuario, clave.get_secret_value()),
                )
            except httpx.TransportError as exc:
                ultimo = exc
                logger.warning(
                    "Fallo de red llamando a Bizlinks (%s/%s): %s",
                    intento + 1,
                    intentos,
                    type(exc).__name__,
                )
                if intento + 1 < intentos:
                    time.sleep(min(0.5 * (2**intento), 4.0))
                continue

            if respuesta.status_code in (401, 403):
                raise BizlinksAuthError(details={"http_status": respuesta.status_code})
            if respuesta.status_code in (502, 503, 504):
                raise BizlinksUnavailableError(
                    f"Bizlinks no está disponible (HTTP {respuesta.status_code})",
                    details={"http_status": respuesta.status_code},
                )
            if not respuesta.content:
                raise BizlinksError(
                    f"Bizlinks respondió HTTP {respuesta.status_code} sin contenido",
                    details={"http_status": respuesta.status_code},
                )
            # Un 500 trae un SOAP Fault que parse_respuesta traduce a un error legible.
            return respuesta.content

        raise BizlinksUnavailableError(
            "No se pudo contactar a Bizlinks (timeout o error de red)",
            details={"error": type(ultimo).__name__ if ultimo else None},
        ) from ultimo
