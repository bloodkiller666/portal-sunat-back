"""Localización de los WSDL de SUNAT.

Estrategia:

* Si `SUNAT_WSDL_DIR` está definido se guarda el WSDL en disco, junto con los
  documentos que importa, y se reescriben los `import`/`include` absolutos para
  que apunten a los archivos locales. Así el WSDL es autocontenido: no se
  descarga en cada arranque y las pruebas no tocan la red.
* Si no, se entrega la URL a zeep, que la descarga y resuelve los imports por su
  cuenta.

Nota: el WSDL real de `billConsultService` no contiene los tipos, solo importa
`...billConsultService?wsdl=1`; sin esta reescritura la caché en disco quedaría
incompleta.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
from lxml import etree

from app.core.config import Settings
from app.core.errors import SunatUnavailableError
from app.core.logging import get_logger
from app.sunat.endpoints import ServicioSunat, wsdl_url

logger = get_logger(__name__)

_ELEMENTOS_CON_IMPORTS = frozenset({"import", "include", "redefine"})


def _imports_absolutos(root: etree._Element) -> list[tuple[etree._Element, str, str]]:
    """Devuelve los imports/includes que apuntan a URLs absolutas.

    Solo se consideran `wsdl:import`/`xsd:import`/`xsd:include`; el
    `soap:address location` es la dirección del servicio, no un documento, y no
    debe reescribirse.
    """
    encontrados: list[tuple[etree._Element, str, str]] = []
    for elemento in root.iter():
        if not isinstance(elemento.tag, str):
            continue
        if etree.QName(elemento).localname not in _ELEMENTOS_CON_IMPORTS:
            continue
        for atributo in ("location", "schemaLocation"):
            valor = elemento.get(atributo)
            if valor and valor.startswith(("http://", "https://")):
                encontrados.append((elemento, atributo, valor))
                break
    return encontrados


class WsdlLoader:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self._dir = settings.sunat_wsdl_dir
        self._client = client
        self._memoria: dict[str, str] = {}

    @property
    def directorio(self) -> Path | None:
        return self._dir

    def resolve(self, servicio: ServicioSunat, wsdl_url: str) -> str:
        """Devuelve la ruta local del WSDL o su URL (para que la descargue zeep)."""
        if self._dir is None:
            return wsdl_url

        destino = self._dir / f"{servicio.value}.wsdl"
        if not destino.exists():
            try:
                self._descargar_arbol(wsdl_url, destino, set())
            except SunatUnavailableError:
                logger.warning(
                    "No se pudo descargar el WSDL de %s; zeep intentará obtenerlo por su cuenta",
                    servicio.value,
                )
                return wsdl_url

        self._memoria[wsdl_url] = str(destino)
        return str(destino)

    # ---------------------------------------------------------------- #
    # Internos
    # ---------------------------------------------------------------- #
    def _descargar_arbol(self, url: str, destino: Path, visitados: set[str]) -> None:
        """Descarga el WSDL, resuelve sus imports a archivos locales y lo guarda."""
        if url in visitados:
            return
        visitados.add(url)

        texto = self._descargar(url)
        contenido = self._reescribir_imports(texto, destino, visitados)
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(contenido, encoding="utf-8")
        logger.info("WSDL de SUNAT guardado localmente (%s)", destino.name)

    def _reescribir_imports(self, texto: str, destino: Path, visitados: set[str]) -> str:
        """Copia los documentos importados junto al WSDL y apunta a ellos."""
        try:
            root = etree.fromstring(texto.encode("utf-8"))
        except etree.XMLSyntaxError:
            logger.warning("El documento descargado no es XML válido; se guarda tal cual")
            return texto

        for elemento, atributo, url_importada in _imports_absolutos(root):
            nombre_local = _nombre_local(url_importada)
            if url_importada not in visitados:
                self._descargar_arbol(url_importada, destino.parent / nombre_local, visitados)
            elemento.set(atributo, nombre_local)
        return etree.tostring(root, encoding="unicode")

    def _descargar(self, url: str) -> str:
        cliente = self._client or httpx.Client(timeout=30.0)
        try:
            response = cliente.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SunatUnavailableError(
                "No se pudo descargar el WSDL de SUNAT", details={"endpoint": url}
            ) from exc
        finally:
            if self._client is None:
                cliente.close()
        return response.text


def _nombre_local(url: str) -> str:
    """Nombre de archivo local, estable y distinguible entre documentos distintos."""
    limpio = url.split("?")[0].rstrip("/")
    base = Path(limpio).name or "documento"
    # sha1 solo sirve para distinguir documentos distintos en el nombre del archivo.
    sufijo = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return f"{base}-{sufijo}.wsdl"


def wsdl_url_for(servicio: ServicioSunat, *, env, settings: Settings | None = None) -> str:
    return wsdl_url(servicio, env=env, settings=settings)
