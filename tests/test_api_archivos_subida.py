"""Subida de archivos de un comprobante: el PDF de la representación impresa."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.enums import EstadoArchivo, TipoArchivo
from app.db.models import Company, Comprobante
from app.services.archivos import TAMANO_MAXIMO_ARCHIVO, inferir_tipo
from tests.conftest import crear_token
from tests.fakes import FakeUnitOfWork, construir_comprobante

PDF = b"%PDF-1.4\n% contenido de prueba\n%%EOF\n"


@pytest.fixture
def comprobante(empresa_principal: Company, uow: FakeUnitOfWork) -> Comprobante:
    creado = construir_comprobante(empresa_principal.id, correlativo=1)
    uow.comprobantes.comprobantes[creado.id] = creado
    return creado


def _ruta(comprobante_id) -> str:
    return f"/api/v1/comprobantes/{comprobante_id}/archivos"


def test_subir_sin_token_devuelve_401(client: TestClient, comprobante: Comprobante) -> None:
    assert client.post(_ruta(comprobante.id), files={"archivo": ("f.pdf", PDF)}).status_code == 401


def test_subir_como_lector_devuelve_403(
    client: TestClient, auth_lector: dict[str, str], comprobante: Comprobante
) -> None:
    respuesta = client.post(
        _ruta(comprobante.id),
        headers=auth_lector,
        files={"archivo": ("f.pdf", PDF, "application/pdf")},
    )
    assert respuesta.status_code == 403


def test_subir_a_comprobante_inexistente(client: TestClient, auth: dict[str, str]) -> None:
    respuesta = client.post(
        _ruta(uuid4()), headers=auth, files={"archivo": ("f.pdf", PDF, "application/pdf")}
    )
    assert respuesta.status_code == 404


def test_sin_r2_configurado_responde_503(
    client: TestClient, auth: dict[str, str], comprobante: Comprobante
) -> None:
    respuesta = client.post(
        _ruta(comprobante.id),
        headers=auth,
        files={"archivo": ("f.pdf", PDF, "application/pdf")},
    )
    assert respuesta.status_code == 503
    assert respuesta.json()["error"]["code"] == "storage_not_configured"


def test_archiva_el_pdf_y_lo_lista(
    cliente_r2: TestClient, uow: FakeUnitOfWork, comprobante: Comprobante
) -> None:
    auth = {"Authorization": f"Bearer {crear_token()}"}

    subida = cliente_r2.post(
        _ruta(comprobante.id),
        headers=auth,
        files={"archivo": ("F001-00000001.pdf", PDF, "application/pdf")},
    )

    assert subida.status_code == 201
    cuerpo = subida.json()
    assert cuerpo["tipo"] == "pdf"
    assert cuerpo["nombre"] == "F001-00000001.pdf"
    assert cuerpo["mime_type"] == "application/pdf"
    assert cuerpo["size_bytes"] == len(PDF)
    assert cuerpo["estado"] == "subido"
    assert cuerpo["hash_sha256"]

    guardado = uow.archivos.archivos[0]
    assert guardado.tipo is TipoArchivo.pdf
    assert guardado.estado is EstadoArchivo.subido

    listado = cliente_r2.get(_ruta(comprobante.id), headers=auth).json()
    assert listado["total"] == 1
    assert listado["items"][0]["id"] == cuerpo["id"]

    descarga = cliente_r2.post(
        f"/api/v1/archivos/{cuerpo['id']}/download-url", headers=auth
    )
    assert descarga.status_code == 200
    assert descarga.json()["url"].startswith("https://firmada/")


def test_el_tipo_declarado_manda_sobre_la_extension(
    cliente_r2: TestClient, comprobante: Comprobante
) -> None:
    auth = {"Authorization": f"Bearer {crear_token()}"}

    respuesta = cliente_r2.post(
        _ruta(comprobante.id),
        headers=auth,
        data={"tipo": "cdr"},
        files={"archivo": ("constancia.pdf", b"PK\x03\x04zip", "application/pdf")},
    )

    assert respuesta.status_code == 201
    assert respuesta.json()["tipo"] == "cdr"


def test_extension_desconocida_sin_tipo_responde_422(
    cliente_r2: TestClient, comprobante: Comprobante
) -> None:
    auth = {"Authorization": f"Bearer {crear_token()}"}

    respuesta = cliente_r2.post(
        _ruta(comprobante.id),
        headers=auth,
        files={"archivo": ("datos.bin", b"\x00\x01", "application/octet-stream")},
    )

    assert respuesta.status_code == 422
    assert respuesta.json()["error"]["code"] == "invalid_document"


def test_archivo_vacio_responde_422(cliente_r2: TestClient, comprobante: Comprobante) -> None:
    auth = {"Authorization": f"Bearer {crear_token()}"}

    respuesta = cliente_r2.post(
        _ruta(comprobante.id),
        headers=auth,
        files={"archivo": ("vacio.pdf", b"", "application/pdf")},
    )

    assert respuesta.status_code == 422


def test_archivo_demasiado_grande_responde_413(
    cliente_r2: TestClient, comprobante: Comprobante, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.routers.archivos.TAMANO_MAXIMO_ARCHIVO", 1024)
    auth = {"Authorization": f"Bearer {crear_token()}"}

    respuesta = cliente_r2.post(
        _ruta(comprobante.id),
        headers=auth,
        files={"archivo": ("grande.pdf", b"x" * 4096, "application/pdf")},
    )

    assert respuesta.status_code == 413
    assert respuesta.json()["error"]["details"]["maximo_bytes"] == 1024
    assert TAMANO_MAXIMO_ARCHIVO > 0


def test_no_se_puede_subir_a_una_compra_de_otra_empresa(
    app_r2: FastAPI, cliente_r2: TestClient, uow: FakeUnitOfWork, empresa_ajena: Company
) -> None:
    ajeno = construir_comprobante(empresa_ajena.id, ruc=empresa_ajena.ruc)
    uow.comprobantes.comprobantes[ajeno.id] = ajeno

    respuesta = cliente_r2.post(
        _ruta(ajeno.id),
        headers={"Authorization": f"Bearer {crear_token()}"},
        files={"archivo": ("f.pdf", PDF, "application/pdf")},
    )

    assert respuesta.status_code == 404


def test_inferencia_de_tipo() -> None:
    assert inferir_tipo("factura.XML", None) is TipoArchivo.xml
    assert inferir_tipo(None, "application/pdf; charset=binary") is TipoArchivo.pdf
    assert inferir_tipo("paquete.zip", "application/zip") is TipoArchivo.zip
    assert inferir_tipo("raro.txt", "text/plain") is None
