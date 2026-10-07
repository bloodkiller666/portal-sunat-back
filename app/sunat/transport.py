"""Transporte SOAP basado en httpx.

zeep usa `requests` por defecto; aquí se reemplaza por httpx para poder mockear
las llamadas en las pruebas, controlar timeouts/reintentos y registrar las tramas.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from zeep.transports import Transport
from zeep.wsdl.utils import etree_to_string

from app.core.errors import SunatUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)


class HttpxSunatTransport(Transport):
    """Transporte zeep que envía las tramas con httpx.Client."""

    def __init__(
        self,
        *,
        timeout: float,
        verify_ssl: bool,
        max_retries: int,
        client: httpx.Client | None = None,
    ) -> None:
        # La firma de `zeep.Transport.__init__` cambia entre versiones: se prueban
        # las variantes conocidas en lugar de fijar una.
        for kwargs in (
            {"timeout": timeout, "operation_timeout": timeout, "verify_ssl": verify_ssl},
            {"timeout": timeout, "operation_timeout": timeout},
            {},
        ):
            try:
                super().__init__(**kwargs)
                break
            except TypeError:
                continue

        self._max_retries = max_retries
        self._client = client or httpx.Client(timeout=timeout, verify=verify_ssl)
        self.last_status_code: int | None = None
        self.last_response_body: bytes | None = None

    # ---------------------------------------------------------------- #
    # API de zeep
    # ---------------------------------------------------------------- #
    def post_xml(  # type: ignore[override]
        self, address: str, envelope: bytes | Any, headers: dict[str, str]
    ) -> httpx.Response:
        """zeep espera el objeto de respuesta: él mismo interpreta el SOAP Fault."""
        # zeep entrega el sobre como Element de lxml; httpx necesita bytes.
        mensaje = (
            envelope if isinstance(envelope, (bytes, bytearray)) else etree_to_string(envelope)
        )
        cabeceras = {"Content-Type": "text/xml; charset=utf-8"}
        if headers:
            cabeceras.update(headers)
        return self._enviar(address, mensaje, cabeceras)

    def post(  # type: ignore[override]
        self, address: str, message: bytes, headers: dict[str, str]
    ) -> httpx.Response:
        return self._enviar(address, message, headers or {})

    def close(self) -> None:
        self._client.close()

    # ---------------------------------------------------------------- #
    # Internos
    # ---------------------------------------------------------------- #
    def _enviar(self, address: str, payload: bytes, headers: dict[str, str]) -> httpx.Response:
        intentos = self._max_retries + 1
        ultimo_error: Exception | None = None

        for intento in range(intentos):
            try:
                response = self._client.post(address, content=payload, headers=headers)
            except httpx.TransportError as exc:
                ultimo_error = exc
                logger.warning(
                    "Fallo de red llamando a SUNAT (%s/%s): %s",
                    intento + 1,
                    intentos,
                    type(exc).__name__,
                )
                if intento + 1 < intentos:
                    time.sleep(min(0.5 * (2**intento), 4.0))
                continue

            self.last_status_code = response.status_code
            self.last_response_body = response.content

            if response.content:
                # El cuerpo (incluido el SOAP Fault de un 500) lo interpreta zeep.
                return response

            raise SunatUnavailableError(
                f"SUNAT respondió con el estado HTTP {response.status_code} sin contenido",
                details={"http_status": response.status_code, "endpoint": address},
            )

        detalle = type(ultimo_error).__name__ if ultimo_error else None
        raise SunatUnavailableError(
            "No se pudo contactar a SUNAT (timeout o error de red)",
            details={"endpoint": address, "error": detalle},
        ) from ultimo_error
