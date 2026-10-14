"""Pruebas del endpoint de detalle `GET /comprobantes/{id}` (cabecera + ítems)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from tests.conftest import RUC_PRINCIPAL
from tests.fakes import FakeUnitOfWork

UBL = Path(__file__).parent / "fixtures" / "ubl"
IMPORT = f"/api/v1/companies/{RUC_PRINCIPAL}/comprobantes/import"


def _xml(nombre: str) -> bytes:
    return (UBL / nombre).read_bytes()


def _importar(
    client: TestClient, auth: dict[str, str], archivo: str = "factura_con_items.xml"
) -> dict:
    respuesta = client.post(
        IMPORT,
        headers=auth,
        files={"archivo": (archivo, _xml(archivo), "application/xml")},
    )
    assert respuesta.status_code == 201
    return respuesta.json()


def test_detalle_sin_token_devuelve_401(client: TestClient) -> None:
    assert client.get(f"/api/v1/comprobantes/{uuid4()}").status_code == 401


def test_detalle_incluye_la_cabecera_y_los_items(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork
) -> None:
    creado = _importar(client, auth)

    respuesta = client.get(f"/api/v1/comprobantes/{creado['id']}", headers=auth)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["nombre_archivo"] == f"{RUC_PRINCIPAL}-01-F001-00000001"
    assert cuerpo["monto_total"] == "188.80"
    assert cuerpo["estado_label"] == "Pendiente"
    assert cuerpo["razon_social_receptor"] == "CLIENTE EJEMPLO S.A.C."

    assert len(cuerpo["items"]) == 2
    primero = cuerpo["items"][0]
    assert primero["item_nro"] == 1
    assert primero["descripcion"] == "Cemento Portland Tipo I"
    assert primero["codigo"] == "PROD-01"
    assert primero["cantidad"] == "2.00"
    assert primero["unidad_medida"] == "NIU"
    assert primero["precio_unitario"] == "59.000000"
    assert primero["igv"] == "18.00"
    assert primero["tip_afectacion_igv"] == "10"
    assert primero["total"] == "100.00"


def test_detalle_de_un_comprobante_sin_items_vuelve_lista_vacia(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork
) -> None:
    creado = _importar(client, auth, archivo="factura.xml")

    cuerpo = client.get(f"/api/v1/comprobantes/{creado['id']}", headers=auth).json()

    assert cuerpo["items"] == []


def test_detalle_como_lector_esta_permitido(
    client: TestClient, auth: dict[str, str], auth_lector: dict[str, str], uow: FakeUnitOfWork
) -> None:
    creado = _importar(client, auth)

    respuesta = client.get(f"/api/v1/comprobantes/{creado['id']}", headers=auth_lector)

    assert respuesta.status_code == 200
    assert len(respuesta.json()["items"]) == 2


def test_detalle_inexistente_devuelve_404(client: TestClient, auth: dict[str, str]) -> None:
    respuesta = client.get(f"/api/v1/comprobantes/{uuid4()}", headers=auth)
    assert respuesta.status_code == 404
    assert respuesta.json()["error"]["code"] == "not_found"
