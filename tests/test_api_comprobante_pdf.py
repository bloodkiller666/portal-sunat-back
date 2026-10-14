"""Pruebas de la descarga del PDF `GET /comprobantes/{id}/pdf`."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.integrations.pdf import PdfRenderer, SunatPdfRenderer
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


def test_el_renderizador_cumple_el_contrato_pdf() -> None:
    assert isinstance(SunatPdfRenderer(), PdfRenderer)


def test_descargar_pdf_genera_un_documento_valido(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork
) -> None:
    creado = _importar(client, auth)

    respuesta = client.get(f"/api/v1/comprobantes/{creado['id']}/pdf", headers=auth)

    assert respuesta.status_code == 200
    assert respuesta.headers["content-type"] == "application/pdf"
    assert respuesta.content.startswith(b"%PDF")
    assert len(respuesta.content) > 500
    disposition = respuesta.headers["content-disposition"]
    assert f"{RUC_PRINCIPAL}-01-F001-00000001.pdf" in disposition


def test_pdf_de_un_comprobante_sin_items_tambien_se_genera(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork
) -> None:
    """El PDF se construye con la cabecera aunque no haya líneas registradas."""
    creado = _importar(client, auth, archivo="factura.xml")

    respuesta = client.get(f"/api/v1/comprobantes/{creado['id']}/pdf", headers=auth)

    assert respuesta.status_code == 200
    assert respuesta.content.startswith(b"%PDF")


def test_pdf_como_lector_esta_permitido(
    client: TestClient, auth: dict[str, str], auth_lector: dict[str, str], uow: FakeUnitOfWork
) -> None:
    creado = _importar(client, auth)

    respuesta = client.get(f"/api/v1/comprobantes/{creado['id']}/pdf", headers=auth_lector)

    assert respuesta.status_code == 200
    assert respuesta.headers["content-type"] == "application/pdf"


def test_pdf_de_un_comprobante_inexistente_devuelve_404(
    client: TestClient, auth: dict[str, str]
) -> None:
    respuesta = client.get(f"/api/v1/comprobantes/{uuid4()}/pdf", headers=auth)
    assert respuesta.status_code == 404


def test_pdf_con_caracteres_especiales_no_falla(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork
) -> None:
    """La descripción con acentos/ñ se acomoda a las fuentes latin-1 del PDF."""
    creado = _importar(client, auth, archivo="nota_credito_con_items.xml")

    respuesta = client.get(f"/api/v1/comprobantes/{creado['id']}/pdf", headers=auth)

    assert respuesta.status_code == 200
    assert respuesta.content.startswith(b"%PDF")
