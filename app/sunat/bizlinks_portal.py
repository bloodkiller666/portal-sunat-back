"""Consulta anónima del portal público de Bizlinks (alternativa a la API `invoker`).

`https://sfe.bizlinks.com.pe/sfeperu/public/loginAnonimo.jsf` permite a cualquiera que
conozca los datos de un comprobante (tipo, serie-número, monto, fecha y RUC del emisor)
consultarlo y descargar su XML y su CDR. Es la misma consulta que haría una persona, pero
automatizada, así que se usa **solo como respaldo** cuando la API del OSE no está
habilitada y con los límites que aplica el servicio `app.services.bizlinks`
(tope diario, pausa entre peticiones y cortacircuito).

Es una página JSF/PrimeFaces con estado de vista (`javax.faces.ViewState`). El flujo
verificado el 2026-10-19 con un comprobante real de la empresa (los archivos descargados
fueron idénticos, byte a byte, a los bajados desde el navegador) es:

1. `GET` del formulario → `ViewState` y el id del botón «Consultar».
2. `POST` ajax del `loginForm` con los cinco datos → respuesta parcial que, si el
   comprobante existe, ordena abrir el diálogo `dlgVerDetalle`.
3. Por cada archivo, `POST` ajax del `downloadForm` (`btnDescargarXML` / `btnDescargarCDR`
   con `tipo=xml|cdr`) → `{"seleccionado":true}`, y luego `POST` del `downloadForm` con
   `downloadForm:_idcl=descargarZip`, que devuelve el ZIP.

Reglas de convivencia, a propósito conservadoras:

* Nunca se intenta saltar un CAPTCHA ni un bloqueo (403/429): se levanta
  `BizlinksBlockedError` y el servicio pausa las consultas.
* Si la página deja de tener la forma esperada, se levanta `BizlinksPortalChangedError`
  en vez de adivinar.
* Una pausa aleatoria separa todas las peticiones; el `User-Agent` es propio y honesto.

El servidor solo habla TLS 1.2 con `AES256-SHA`, que OpenSSL 3 desactiva por defecto
(nivel de seguridad 2). Con `bizlinks_portal_tls_compat` se baja el nivel a 1 **solo para
este cliente**; el certificado se sigue verificando. Los datos que viajan son los de un
comprobante ya emitido (no credenciales).
"""

from __future__ import annotations

import io
import random
import re
import ssl
import time
import zipfile
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from urllib.parse import urlparse

import httpx
from lxml import etree, html

from app.core.config import Settings
from app.core.errors import (
    BizlinksBlockedError,
    BizlinksError,
    BizlinksPortalChangedError,
    BizlinksUnavailableError,
)
from app.core.logging import get_logger
from app.secrets.base import CompanyCredentials
from app.sunat.bizlinks import TAMANO_MAXIMO_DESCARGA, RespaldosBizlinks, host_permitido

logger = get_logger(__name__)

TIPOS_SOPORTADOS = frozenset({"01", "03", "07", "08"})
TAMANO_MAXIMO_XML = 5 * 1024 * 1024  # mismo tope que la importación de XML

_SERIE_NUMERO_RE = re.compile(r"^[A-Z0-9]{4}-[0-9]{1,8}$")
_CAMPOS_FORMULARIO = (
    "Documento_input",
    "nuSerieNumero",
    "montoImporteTotal",
    "fechaEmision_input",
    "nuTributarioEmisor",
)
_VIEWSTATE_RE = re.compile(r'<update id="javax\.faces\.ViewState"><!\[CDATA\[(.*?)\]\]>', re.S)
_ARGS_RE = re.compile(r'<extension[^>]*type="args"[^>]*>(.*?)</extension>', re.S)


def _contexto_tls(compatible: bool) -> ssl.SSLContext:
    contexto = ssl.create_default_context()
    if compatible:
        contexto.set_ciphers("DEFAULT:@SECLEVEL=1")
    return contexto


class BizlinksPortalClient:
    """Cliente del portal anónimo. Una instancia mantiene la sesión (cookies) y el ritmo."""

    canal = "portal"

    def __init__(
        self,
        credentials: CompanyCredentials,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        aleatorio: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._url = settings.bizlinks_portal_url
        if not host_permitido(self._url, settings.bizlinks_download_hosts):
            raise BizlinksError(
                "La URL del portal de Bizlinks no pertenece a un dominio permitido",
                details={"anfitrion": urlparse(self._url).hostname},
            )
        self._ruc = credentials.ruc
        self._minimo = settings.bizlinks_portal_min_interval_seconds
        self._maximo = max(self._minimo, settings.bizlinks_portal_max_interval_seconds)
        self._sleep = sleep
        self._aleatorio = aleatorio
        self._peticiones = 0
        self._client = client or httpx.Client(
            verify=_contexto_tls(settings.bizlinks_portal_tls_compat),
            timeout=settings.sunat_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": settings.bizlinks_portal_user_agent},
        )
        self.ultimo_http_status: int | None = None

    # ---------------------------------------------------------------- #
    # API común con BizlinksClient
    # ---------------------------------------------------------------- #
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
        if fecha_emision is None or monto_total is None:
            return RespaldosBizlinks(
                encontrado=False,
                avisos=("El portal de Bizlinks exige la fecha de emisión y el monto total",),
            )
        if tipo_documento not in TIPOS_SOPORTADOS:
            return RespaldosBizlinks(
                encontrado=False, avisos=(f"Tipo de documento no soportado: {tipo_documento}",)
            )
        serie_numero = f"{serie.upper()}-{correlativo:08d}"
        if not _SERIE_NUMERO_RE.match(serie_numero):
            raise BizlinksError("La serie o el correlativo no son válidos para consultar")

        formulario = self._abrir_formulario()
        encontrado, aviso, vista = self._consultar(
            formulario,
            tipo_documento=tipo_documento,
            serie_numero=serie_numero,
            monto_total=monto_total,
            fecha_emision=fecha_emision,
        )
        if not encontrado:
            return RespaldosBizlinks(encontrado=False, avisos=(aviso,) if aviso else ())

        avisos: list[str] = []
        xml = cdr = None
        if necesita_xml:
            zip_xml, vista = self._descargar("xml", vista, avisos)
            xml = _extraer_xml(zip_xml, avisos) if zip_xml else None
        if necesita_cdr:
            cdr, vista = self._descargar("cdr", vista, avisos)
        return RespaldosBizlinks(encontrado=True, xml=xml, cdr=cdr, avisos=tuple(avisos))

    # ---------------------------------------------------------------- #
    # Pasos del flujo
    # ---------------------------------------------------------------- #
    def _abrir_formulario(self) -> dict[str, str]:
        respuesta = self._peticion("GET", self._url)
        texto = respuesta.text
        if "captcha" in texto.lower():
            raise BizlinksBlockedError("Bizlinks pide un CAPTCHA: se detienen las consultas")
        try:
            documento = html.fromstring(respuesta.content)
            vista = documento.xpath("//input[@name='javax.faces.ViewState']/@value")[0]
            botones = documento.xpath(
                "//button[@type='submit'][contains(@onclick, \"formId:'loginForm'\")]/@id"
            )
            faltantes = [
                campo
                for campo in _CAMPOS_FORMULARIO
                if not documento.xpath(f"//*[@name='{campo}']")
            ]
        except (etree.ParserError, IndexError) as exc:
            raise BizlinksPortalChangedError() from exc
        if not botones or faltantes:
            raise BizlinksPortalChangedError(details={"faltantes": faltantes})
        return {"vista": vista, "boton": botones[0]}

    def _consultar(
        self,
        formulario: dict[str, str],
        *,
        tipo_documento: str,
        serie_numero: str,
        monto_total: Decimal,
        fecha_emision: date,
    ) -> tuple[bool, str | None, str]:
        boton = formulario["boton"]
        respuesta = self._peticion(
            "POST",
            self._url,
            data={
                "loginForm_SUBMIT": "1",
                "javax.faces.ViewState": formulario["vista"],
                "Documento_input": tipo_documento,
                "nuSerieNumero": serie_numero,
                "montoImporteTotal": f"{monto_total:.2f}",
                "fechaEmision_input": fecha_emision.strftime("%d/%m/%Y"),
                "nuTributarioEmisor": self._ruc,
                "javax.faces.partial.ajax": "true",
                "javax.faces.source": boton,
                "javax.faces.partial.execute": "@all",
                "javax.faces.partial.render": "dlgVerDetalle growl dlgMessageErrors",
                boton: boton,
            },
            ajax=True,
        )
        contenido = respuesta.content
        vista = _viewstate(contenido)
        evaluaciones = " ".join(
            e.text or "" for e in _raiz(contenido).iter("eval") if isinstance(e.tag, str)
        )
        if "dlgVerDetalle.show" in evaluaciones:
            if vista is None:
                raise BizlinksPortalChangedError()
            return True, None, vista
        if "dlgMessageErrors.show" in evaluaciones:
            return False, _mensaje_de_error(contenido) or "Bizlinks no encontró el comprobante", ""
        # Ni detalle ni error: la página respondió algo que no conocemos.
        raise BizlinksPortalChangedError()

    def _descargar(self, tipo: str, vista: str, avisos: list[str]) -> tuple[bytes | None, str]:
        boton = "btnDescargarXML" if tipo == "xml" else "btnDescargarCDR"
        paso = self._peticion(
            "POST",
            self._url,
            data={
                "downloadForm_SUBMIT": "1",
                "javax.faces.ViewState": vista,
                "javax.faces.partial.ajax": "true",
                "javax.faces.source": boton,
                "javax.faces.partial.execute": "@all",
                boton: boton,
                "tipo": tipo,
            },
            ajax=True,
        )
        vista = _viewstate(paso.content) or vista
        if '"seleccionado":true' not in _args(paso.content).replace(" ", ""):
            avisos.append(f"Bizlinks no ofrece el {tipo.upper()} de este comprobante")
            return None, vista

        archivo = self._peticion(
            "POST",
            self._url,
            data={
                "downloadForm_SUBMIT": "1",
                "javax.faces.ViewState": vista,
                "downloadForm:_idcl": "descargarZip",
            },
        )
        contenido = archivo.content
        if len(contenido) > TAMANO_MAXIMO_DESCARGA:
            raise BizlinksError("El archivo de Bizlinks supera el tamaño permitido")
        if not contenido.startswith(b"PK"):
            # Esperábamos un ZIP: lo que llegó no es lo que descarga el botón.
            raise BizlinksPortalChangedError(details={"descarga": tipo})
        return contenido, vista

    # ---------------------------------------------------------------- #
    # HTTP
    # ---------------------------------------------------------------- #
    def _peticion(
        self,
        metodo: str,
        url: str,
        *,
        data: dict[str, str] | None = None,
        ajax: bool = False,
    ) -> httpx.Response:
        if self._peticiones > 0:
            self._sleep(self._aleatorio(self._minimo, self._maximo))
        self._peticiones += 1
        cabeceras = {"Referer": self._url}
        if ajax:
            cabeceras.update(
                {"Faces-Request": "partial/ajax", "X-Requested-With": "XMLHttpRequest"}
            )
        try:
            respuesta = self._client.request(metodo, url, data=data, headers=cabeceras)
        except httpx.TransportError as exc:
            raise BizlinksUnavailableError(
                "No se pudo contactar al portal de Bizlinks",
                details={"error": type(exc).__name__},
            ) from exc
        self.ultimo_http_status = respuesta.status_code
        if respuesta.status_code in (403, 429):
            raise BizlinksBlockedError(details={"http_status": respuesta.status_code})
        if respuesta.status_code >= 500:
            raise BizlinksUnavailableError(
                f"El portal de Bizlinks respondió HTTP {respuesta.status_code}",
                details={"http_status": respuesta.status_code},
            )
        if respuesta.status_code >= 400:
            raise BizlinksPortalChangedError(details={"http_status": respuesta.status_code})
        return respuesta


# --------------------------------------------------------------------------- #
# Interpretación de las respuestas parciales de JSF
# --------------------------------------------------------------------------- #
def _raiz(contenido: bytes) -> etree._Element:
    try:
        return etree.fromstring(contenido)
    except etree.XMLSyntaxError as exc:
        raise BizlinksPortalChangedError() from exc


def _viewstate(contenido: bytes) -> str | None:
    coincidencia = _VIEWSTATE_RE.search(contenido.decode("utf-8", "replace"))
    return coincidencia.group(1) if coincidencia else None


def _args(contenido: bytes) -> str:
    coincidencia = _ARGS_RE.search(contenido.decode("utf-8", "replace"))
    return coincidencia.group(1) if coincidencia else ""


def _mensaje_de_error(contenido: bytes) -> str | None:
    """Texto del diálogo de errores (`dlgMessageErrors`), sin etiquetas ni JavaScript.

    La actualización trae HTML, comentarios con `<script>` de PrimeFaces y el botón
    «Aceptar»; se queda solo el texto que ve la persona (p. ej. «Los datos ingresados no
    corresponden a ningún documento»).
    """
    for actualizacion in _raiz(contenido).iter("update"):
        if actualizacion.get("id") != "dlgMessageErrors" or not actualizacion.text:
            continue
        crudo = re.sub(r"(?is)<!--.*?-->", " ", actualizacion.text)
        crudo = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", crudo)
        try:
            texto = html.fromstring(crudo).text_content()
        except (etree.ParserError, ValueError):
            texto = crudo
        limpio = re.sub(r"\s+", " ", texto).strip()
        # La cabecera del diálogo dice «Información»/«Error» antes del mensaje real.
        limpio = re.sub(r"^(Informaci[oó]n|Error|Atenci[oó]n)\s*[:.]?\s*", "", limpio)
        limpio = limpio.removesuffix("Aceptar").strip()
        return limpio[:300] or None
    return None


def _extraer_xml(contenido: bytes, avisos: list[str]) -> bytes | None:
    """El XML del ZIP que entrega el portal (un solo `.xml`, con tope de tamaño)."""
    try:
        with zipfile.ZipFile(io.BytesIO(contenido)) as archivo:
            entradas = [i for i in archivo.infolist() if i.filename.lower().endswith(".xml")]
            if not entradas:
                avisos.append("El ZIP del XML de Bizlinks no contiene ningún .xml")
                return None
            entrada = entradas[0]
            if entrada.file_size > TAMANO_MAXIMO_XML:
                avisos.append("El XML de Bizlinks supera el tamaño permitido")
                return None
            return archivo.read(entrada)
    except zipfile.BadZipFile:
        avisos.append("El ZIP del XML de Bizlinks está dañado")
        return None
