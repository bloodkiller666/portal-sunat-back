"""Filtros del listado de comprobantes (fechas, tipo, serie y búsqueda)."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.enums import EstadoComprobante
from app.db.models import Company
from tests.conftest import RUC_PRINCIPAL
from tests.fakes import FakeUnitOfWork, construir_comprobante

LISTA = f"/api/v1/companies/{RUC_PRINCIPAL}/comprobantes"


def _poblar(uow: FakeUnitOfWork, empresa: Company) -> None:
    datos = [
        # (tipo, serie, correlativo, fecha, estado)
        ("01", "F001", 1, date(2026, 10, 1), EstadoComprobante.pendiente),
        ("01", "F001", 2, date(2026, 10, 5), EstadoComprobante.aprobado),
        ("03", "B001", 1, date(2026, 10, 5), EstadoComprobante.pendiente),
        ("01", "F002", 7, date(2026, 9, 30), EstadoComprobante.pendiente),
    ]
    for tipo, serie, correlativo, fecha, estado in datos:
        comprobante = construir_comprobante(
            empresa.id,
            ruc=RUC_PRINCIPAL,
            tipo_documento=tipo,
            serie=serie,
            correlativo=correlativo,
            fecha_emision=fecha,
            estado=estado,
        )
        uow.comprobantes.comprobantes[uuid4()] = comprobante


def test_filtra_por_rango_de_fechas(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    _poblar(uow, empresa_principal)

    cuerpo = client.get(
        LISTA, headers=auth, params={"fecha_desde": "2026-10-01", "fecha_hasta": "2026-10-05"}
    ).json()

    assert len(cuerpo["items"]) == 3
    assert all(item["fecha_emision"] >= "2026-10-01" for item in cuerpo["items"])


def test_filtra_por_tipo_y_serie(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    _poblar(uow, empresa_principal)

    por_tipo = client.get(LISTA, headers=auth, params={"tipo_documento": "03"}).json()
    assert len(por_tipo["items"]) == 1
    assert por_tipo["items"][0]["serie"] == "B001"

    por_serie = client.get(LISTA, headers=auth, params={"serie": "F002"}).json()
    assert len(por_serie["items"]) == 1
    assert por_serie["items"][0]["correlativo"] == 7


def test_busca_por_numero_de_documento(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    _poblar(uow, empresa_principal)

    cuerpo = client.get(LISTA, headers=auth, params={"q": "F001-0000"}).json()

    assert len(cuerpo["items"]) == 2
    assert all(item["serie"] == "F001" for item in cuerpo["items"])


def test_combina_filtros(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    _poblar(uow, empresa_principal)

    cuerpo = client.get(
        LISTA,
        headers=auth,
        params={
            "estado": "pendiente",
            "fecha_desde": "2026-10-01",
            "tipo_documento": "01",
            "serie": "F001",
        },
    ).json()

    assert len(cuerpo["items"]) == 1
    assert cuerpo["items"][0]["numero"] == "00000001"


def test_rango_de_fechas_invertido_es_invalido(
    client: TestClient, auth: dict[str, str]
) -> None:
    respuesta = client.get(
        LISTA, headers=auth, params={"fecha_desde": "2026-10-10", "fecha_hasta": "2026-10-01"}
    )

    assert respuesta.status_code == 422
    assert respuesta.json()["error"]["code"] == "validation_error"


def test_tipo_de_documento_con_formato_invalido(client: TestClient, auth: dict[str, str]) -> None:
    respuesta = client.get(LISTA, headers=auth, params={"tipo_documento": "factura"})
    assert respuesta.status_code == 422


def test_busqueda_demasiado_corta(client: TestClient, auth: dict[str, str]) -> None:
    # `q` de un solo carácter obligaría a un escaneo completo por cada consulta.
    assert client.get(LISTA, headers=auth, params={"q": "a"}).status_code == 422


def test_lector_puede_filtrar(
    client: TestClient, auth_lector: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    _poblar(uow, empresa_principal)

    respuesta = client.get(LISTA, headers=auth_lector, params={"serie": "F001"})

    assert respuesta.status_code == 200
    assert len(respuesta.json()["items"]) == 2
