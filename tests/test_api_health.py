from __future__ import annotations

from fastapi.testclient import TestClient

from tests.fakes import FakeSunatClient, FakeUnitOfWork


def test_health_no_exige_token(client: TestClient) -> None:
    respuesta = client.get("/health")
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["status"] == "ok"
    assert cuerpo["sunat_env"] == "beta"
    assert cuerpo["confirm_prod_emission"] is False
    assert cuerpo["auth_configured"] is True
    assert cuerpo["database_configured"] is False


def test_health_no_toca_la_base_ni_sunat(
    client: TestClient, uow: FakeUnitOfWork, sunat: FakeSunatClient
) -> None:
    client.get("/health")
    assert sunat.llamadas == []
    assert uow.claims == {}


def test_docs_y_openapi_disponibles(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200
    esquema = client.get("/openapi.json").json()
    assert set(esquema["paths"]) == {
        "/health",
        "/health/ready",
        "/api/v1/companies",
        "/api/v1/companies/{ruc}/sunat/status",
        "/api/v1/companies/{ruc}/comprobantes",
        "/api/v1/companies/{ruc}/comprobantes/resumen",
        "/api/v1/companies/{ruc}/comprobantes/import",
        "/api/v1/companies/{ruc}/comprobantes/refresh",
        "/api/v1/companies/{ruc}/comprobantes/import-paquete",
        "/api/v1/companies/{ruc}/comprobantes/import-proveedores",
        "/api/v1/companies/{ruc}/comprobantes/import-proveedores/archivos",
        "/api/v1/companies/{ruc}/comprobantes/import-cdr",
        "/api/v1/companies/{ruc}/comprobantes/import-sap",
        "/api/v1/comprobantes/{comprobante_id}",
        "/api/v1/carpeta-local/abrir",
        "/api/v1/comprobantes/{comprobante_id}/refresh-cdr",
        "/api/v1/comprobantes/{comprobante_id}/envios",
        "/api/v1/comprobantes/{comprobante_id}/archivos",
        "/api/v1/archivos/{archivo_id}/download-url",
        "/api/v1/companies/{ruc}/sire/ventas/sync",
        "/api/v1/companies/{ruc}/sire/compras/sync",
        "/api/v1/companies/{ruc}/sire/evidencias",
        "/api/v1/companies/{ruc}/bizlinks/respaldos/sync",
    }


def test_cors_responde_al_origen_configurado(client: TestClient) -> None:
    respuesta = client.options(
        "/health",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert respuesta.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_se_incluye_el_identificador_de_peticion(client: TestClient) -> None:
    respuesta = client.get("/health", headers={"X-Request-Id": "abc-123"})
    assert respuesta.headers["X-Request-Id"] == "abc-123"
