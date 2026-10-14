"""Las llamadas a SUNAT no pueden bloquear el event loop.

`zeep` es síncrono: si se invoca directamente desde una corrutina, el worker se
queda congelado durante el timeout (20 s + reintentos) y ninguna otra petición
—ni el `/health`— se atiende. Estas pruebas fallan si alguien vuelve a llamar al
cliente SUNAT sin pasar por `run_blocking`.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.concurrency import run_blocking
from app.core.enums import RolEmpresa
from app.core.security import AuthenticatedUser
from app.db.models import Company, CompanyMember
from app.services.sunat_status import check_company_sunat_status
from tests.conftest import RUC_PRINCIPAL, USUARIO_ID
from tests.fakes import (
    FakeSunatClient,
    FakeUnitOfWork,
    disponibilidad_autenticado,
    fabricar_cliente_sunat,
)

RETARDO = 0.4
RUTA_STATUS = f"/api/v1/companies/{RUC_PRINCIPAL}/sunat/status"


class ClienteLento(FakeSunatClient):
    """Cliente SUNAT simulado que tarda lo que tardaría una llamada real."""

    def check_availability(self) -> Any:
        time.sleep(RETARDO)
        return super().check_availability()

    def get_status_cdr(self, **kwargs: Any) -> Any:
        time.sleep(RETARDO)
        return super().get_status_cdr(**kwargs)


async def test_run_blocking_libera_el_event_loop() -> None:
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    tarea = asyncio.create_task(ticker())
    await run_blocking(time.sleep, RETARDO)
    tarea.cancel()

    # Si `time.sleep` hubiera corrido en el loop, el ticker no habría avanzado.
    assert ticks >= RETARDO * 100 * 0.5


async def test_sunat_status_no_bloquea_el_event_loop(
    settings: Any,
    uow: FakeUnitOfWork,
    empresa_principal: Company,
    app: FastAPI,
) -> None:
    cliente = ClienteLento(disponibilidad=disponibilidad_autenticado())
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    tarea = asyncio.create_task(ticker())
    resultado = await check_company_sunat_status(
        uow=uow,
        settings=settings,
        secrets_provider=app.state.secrets_provider,
        client_factory=fabricar_cliente_sunat(cliente),
        user=AuthenticatedUser(id=USUARIO_ID),
        access=CompanyMember(company=empresa_principal, rol=RolEmpresa.emisor),
    )
    tarea.cancel()

    assert resultado.estado == "autenticado"
    assert ticks >= RETARDO * 100 * 0.5


def test_una_consulta_lenta_no_retrasa_el_health(
    app: FastAPI, client: TestClient, auth: dict[str, str]
) -> None:
    """Prueba de extremo a extremo: con SUNAT tardando, `/health` responde al instante."""
    app.state.sunat_client_factory = fabricar_cliente_sunat(
        ClienteLento(disponibilidad=disponibilidad_autenticado())
    )
    respuesta_lenta: dict[str, Any] = {}

    def consultar_status() -> None:
        respuesta_lenta["respuesta"] = client.get(RUTA_STATUS, headers=auth)

    hilo = threading.Thread(target=consultar_status)
    hilo.start()
    time.sleep(0.05)  # que la consulta lenta ya esté en curso

    inicio = time.perf_counter()
    salud = client.get("/health")
    duracion_ms = (time.perf_counter() - inicio) * 1000

    hilo.join(timeout=5)

    assert salud.status_code == 200
    assert respuesta_lenta["respuesta"].status_code == 200
    assert duracion_ms < RETARDO * 1000 * 0.5
