"""Apartado «Logs»: corridas de la automatización diaria y su estado."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.db.models import AutomatizacionLogRegistro, Company
from tests.conftest import RUC_PRINCIPAL
from tests.fakes import FakeUnitOfWork

RUTA = f"/api/v1/companies/{RUC_PRINCIPAL}/automatizacion/logs"


async def _corrida(
    uow: FakeUnitOfWork, empresa: Company, resultado: str, minutos: int = 0, **campos
) -> None:
    log = await uow.automatizacion_logs.registrar(
        AutomatizacionLogRegistro(company_id=empresa.id, resultado=resultado, **campos)
    )
    log.created_at = datetime(2026, 10, 22, 9, 0, tzinfo=UTC) + timedelta(minutes=minutos)


def test_sin_corridas_responde_vacio_y_sin_estado(
    client: TestClient, auth: dict[str, str], empresa_principal: Company
) -> None:
    cuerpo = client.get(RUTA, headers=auth).json()

    assert cuerpo == {"items": [], "total": 0, "estado": None}


def test_lista_las_mas_recientes_primero_con_su_detalle(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    import asyncio

    asyncio.run(_corrida(uow, empresa_principal, "ok", 0, tareas=["sire", "correo"]))
    asyncio.run(
        _corrida(
            uow,
            empresa_principal,
            "con_novedades",
            240,
            novedades={"PDF unidos": 3},
            detalle={"importacion": {"nuevas": 1}},
            duracion_segundos=53,
        )
    )

    cuerpo = client.get(RUTA, headers=auth).json()

    assert cuerpo["total"] == 2
    assert [i["resultado"] for i in cuerpo["items"]] == ["con_novedades", "ok"]
    primero = cuerpo["items"][0]
    assert primero["novedades"] == {"PDF unidos": 3}
    assert primero["detalle"] == {"importacion": {"nuevas": 1}}
    assert primero["duracion_segundos"] == 53


def test_el_estado_sale_de_la_foto_de_la_ultima_corrida(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    import asyncio

    asyncio.run(
        _corrida(
            uow,
            empresa_principal,
            "con_errores",
            10,
            errores=["SIRE ventas 202610: sunat_auth_error"],
            estado={"ultima_sire_ok": "2026-10-21T09:01:00"},
            pendientes={"carpetas": ["2026/10/2099/F010-00000456"], "sin_clasificar": ["x"]},
        )
    )

    estado = client.get(RUTA, headers=auth).json()["estado"]

    assert estado["ultimo_resultado"] == "con_errores"
    assert estado["ultima_sire_ok"] == "2026-10-21T09:01:00"
    assert estado["carpetas_pendientes"] == ["2026/10/2099/F010-00000456"]
    assert estado["sueltos_pendientes"] == ["x"]


def test_filtra_por_resultado_pero_el_estado_sigue_siendo_el_de_la_ultima_corrida(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    import asyncio

    asyncio.run(_corrida(uow, empresa_principal, "con_errores", 0, errores=["boom"]))
    asyncio.run(_corrida(uow, empresa_principal, "ok", 60))

    cuerpo = client.get(RUTA, headers=auth, params={"resultado": "con_errores"}).json()

    assert [i["resultado"] for i in cuerpo["items"]] == ["con_errores"]
    assert cuerpo["total"] == 1
    assert cuerpo["estado"]["ultimo_resultado"] == "ok"


def test_pagina_con_limit_y_offset(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    import asyncio

    for minuto in range(5):
        asyncio.run(_corrida(uow, empresa_principal, "ok", minuto))

    pagina = client.get(RUTA, headers=auth, params={"limit": 2, "offset": 2}).json()

    assert pagina["total"] == 5 and len(pagina["items"]) == 2


def test_un_lector_puede_verlos_y_otra_empresa_no(
    client: TestClient,
    auth_lector: dict[str, str],
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    empresa_principal: Company,
    empresa_ajena: Company,
) -> None:
    import asyncio

    asyncio.run(_corrida(uow, empresa_principal, "ok"))
    asyncio.run(_corrida(uow, empresa_ajena, "con_errores", errores=["ajeno"]))

    propia = client.get(RUTA, headers=auth_lector).json()
    ajena = client.get(f"/api/v1/companies/{empresa_ajena.ruc}/automatizacion/logs", headers=auth)

    assert [i["resultado"] for i in propia["items"]] == ["ok"]  # no ve lo de otra empresa
    assert ajena.status_code in (403, 404)


def test_exige_token_y_valida_los_parametros(client: TestClient, auth: dict[str, str]) -> None:
    assert client.get(RUTA).status_code == 401
    assert client.get(RUTA, headers=auth, params={"resultado": "otro"}).status_code == 422
    assert client.get(RUTA, headers=auth, params={"limit": 0}).status_code == 422
