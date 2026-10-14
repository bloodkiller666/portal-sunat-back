"""Comprobantes recibidos (compras): importación, filtro y límites."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.core.enums import EstadoComprobante, OrigenComprobante
from app.db.models import Company
from tests.conftest import RUC_PRINCIPAL
from tests.fakes import (
    FakeSunatClient,
    FakeUnitOfWork,
    construir_comprobante,
    resultado_con_cdr,
)

UBL = Path(__file__).parent / "fixtures" / "ubl"
IMPORT = f"/api/v1/companies/{RUC_PRINCIPAL}/comprobantes/import"
LISTA = f"/api/v1/companies/{RUC_PRINCIPAL}/comprobantes"
RUC_PROVEEDOR = "20999999999"


def _xml(nombre: str) -> bytes:
    return (UBL / nombre).read_bytes()


def _importar(client: TestClient, headers: dict[str, str], nombre: str, **datos: str):
    return client.post(
        IMPORT,
        headers=headers,
        data=datos,
        files={"archivo": (nombre, _xml(nombre), "application/xml")},
    )


def test_importar_compra_guarda_emisor_y_adquirente(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    respuesta = _importar(client, auth, "factura_recibida.xml", origen="recibido")

    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    assert cuerpo["origen"] == "recibido"
    assert cuerpo["origen_label"] == "Recibido"
    assert cuerpo["ruc_emisor"] == RUC_PROVEEDOR
    assert cuerpo["ruc_receptor"] == RUC_PRINCIPAL
    assert cuerpo["razon_social_emisor"] == "PROVEEDOR INDUSTRIAL SAC"
    assert cuerpo["razon_social_receptor"] == "ACME SAC"
    assert cuerpo["monto_total"] == "2360.00"
    assert cuerpo["fecha_emision"] == "2026-10-08"

    guardado = next(iter(uow.comprobantes.comprobantes.values()))
    assert guardado.origen is OrigenComprobante.recibido
    assert guardado.company_id == empresa_principal.id


def test_la_misma_factura_emitida_y_recibida_conviven(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork
) -> None:
    """Factura a un cliente con la misma numeración que nos emitió un proveedor."""
    propia = _importar(client, auth, "factura_recibida.xml")
    assert propia.status_code == 422  # como emitida no es nuestra

    # Se emite una factura propia con la serie/numeración F001-1.
    assert _importar(client, auth, "factura.xml").status_code == 201
    # Y la compra del proveedor con la MISMA serie y correlativo también entra.
    compra = _importar(client, auth, "factura_recibida.xml", origen="recibido")
    assert compra.status_code == 201

    origenes = {c.origen for c in uow.comprobantes.comprobantes.values()}
    assert origenes == {OrigenComprobante.emitido, OrigenComprobante.recibido}


def test_compra_a_otro_adquirente_se_rechaza(
    client: TestClient, auth: dict[str, str]
) -> None:
    # El adquirente del XML es otra empresa: no es una compra de esta empresa.
    xml = _xml("factura_recibida.xml").replace(b"20123456789", b"20111111111")

    respuesta = client.post(
        IMPORT,
        headers=auth,
        data={"origen": "recibido"},
        files={"archivo": ("otra.xml", xml, "application/xml")},
    )

    assert respuesta.status_code == 422
    error = respuesta.json()["error"]
    assert error["code"] == "invalid_document"
    assert error["details"]["ruc_receptor"] == "20111111111"


def test_la_propia_factura_no_entra_como_compra(
    client: TestClient, auth: dict[str, str]
) -> None:
    respuesta = _importar(client, auth, "factura.xml", origen="recibido")

    assert respuesta.status_code == 422
    assert "emitido" in respuesta.json()["error"]["message"]


def test_boleta_con_dni_se_archiva_sin_ruc_de_adquirente(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork
) -> None:
    respuesta = _importar(client, auth, "boleta_dni.xml", origen="recibido")

    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    assert cuerpo["tipo_documento"] == "03"
    assert cuerpo["ruc_receptor"] is None
    assert cuerpo["razon_social_receptor"] == "JUAN PEREZ"

    guardado = next(iter(uow.comprobantes.comprobantes.values()))
    assert guardado.receptor_doc == "45678912"


def test_origen_invalido_en_el_import(client: TestClient, auth: dict[str, str]) -> None:
    respuesta = _importar(client, auth, "factura_recibida.xml", origen="compra")
    assert respuesta.status_code == 422


def test_origen_invalido_en_el_listado(client: TestClient, auth: dict[str, str]) -> None:
    assert client.get(LISTA, headers=auth, params={"origen": "compras"}).status_code == 422


def test_listado_separa_ventas_y_compras(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    _importar(client, auth, "factura.xml")
    _importar(client, auth, "factura_recibida.xml", origen="recibido")

    ventas = client.get(LISTA, headers=auth, params={"origen": "emitido"}).json()
    compras = client.get(LISTA, headers=auth, params={"origen": "recibido"}).json()

    assert ventas["total_items" if "total_items" in ventas else "limit"] is not None
    assert len(ventas["items"]) == 1
    assert ventas["items"][0]["origen"] == "emitido"
    assert len(compras["items"]) == 1
    assert compras["items"][0]["origen"] == "recibido"
    assert compras["items"][0]["ruc_emisor"] == RUC_PROVEEDOR

    todas = client.get(LISTA, headers=auth).json()
    assert len(todas["items"]) == 2


def test_no_se_puede_sincronizar_el_cdr_de_una_compra(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    compra = construir_comprobante(
        empresa_principal.id,
        ruc=RUC_PROVEEDOR,
        origen=OrigenComprobante.recibido,
        correlativo=456,
    )
    uow.comprobantes.comprobantes[compra.id] = compra

    respuesta = client.post(f"/api/v1/comprobantes/{compra.id}/refresh-cdr", headers=auth)

    assert respuesta.status_code == 409
    assert "proveedor" in respuesta.json()["error"]["message"]


def test_el_lote_de_pendientes_ignora_las_compras(
    client: TestClient,
    auth: dict[str, str],
    sunat: FakeSunatClient,
    uow: FakeUnitOfWork,
    empresa_principal: Company,
) -> None:
    venta = construir_comprobante(empresa_principal.id, ruc=RUC_PRINCIPAL, correlativo=1)
    compra = construir_comprobante(
        empresa_principal.id,
        ruc=RUC_PROVEEDOR,
        origen=OrigenComprobante.recibido,
        correlativo=456,
        estado=EstadoComprobante.pendiente,
    )
    uow.comprobantes.comprobantes[venta.id] = venta
    uow.comprobantes.comprobantes[compra.id] = compra
    sunat.resultado = resultado_con_cdr()

    cuerpo = client.post(
        f"/api/v1/companies/{RUC_PRINCIPAL}/comprobantes/refresh", headers=auth
    ).json()

    assert cuerpo["total"] == 1
    assert len(sunat.llamadas) == 1
