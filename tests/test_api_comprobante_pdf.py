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


# --------------------------------------------------------------------------- #
# Copia informativa en R2
# --------------------------------------------------------------------------- #
def _importar_r2(cliente: TestClient, auth: dict[str, str]) -> dict:
    respuesta = cliente.post(
        IMPORT,
        headers=auth,
        files={"archivo": ("f.xml", _xml("factura_con_items.xml"), "application/xml")},
    )
    assert respuesta.status_code == 201
    return respuesta.json()


def _pdfs_archivados(uow: FakeUnitOfWork) -> list:
    return [a for a in uow.archivos.archivos if a.tipo.value == "pdf"]


def test_la_primera_descarga_archiva_el_pdf_una_sola_vez(
    cliente_r2: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, almacenamiento
) -> None:
    creado = _importar_r2(cliente_r2, auth)
    url = f"/api/v1/comprobantes/{creado['id']}/pdf"

    primera = cliente_r2.get(url, headers=auth)
    segunda = cliente_r2.get(url, headers=auth)

    assert primera.status_code == segunda.status_code == 200
    pdfs = _pdfs_archivados(uow)
    assert len(pdfs) == 1
    assert pdfs[0].nombre == f"{RUC_PRINCIPAL}-01-F001-00000001.pdf"
    assert pdfs[0].comprobante_id is not None
    # Lo archivado es exactamente lo que se descargó la primera vez.
    import hashlib

    assert pdfs[0].hash_sha256 == hashlib.sha256(primera.content).hexdigest()


def test_el_lector_descarga_pero_no_archiva(
    cliente_r2: TestClient,
    auth: dict[str, str],
    auth_lector: dict[str, str],
    uow: FakeUnitOfWork,
) -> None:
    creado = _importar_r2(cliente_r2, auth)

    respuesta = cliente_r2.get(f"/api/v1/comprobantes/{creado['id']}/pdf", headers=auth_lector)

    assert respuesta.status_code == 200
    assert not _pdfs_archivados(uow)


def test_sin_r2_el_pdf_se_descarga_igual(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork
) -> None:
    creado = _importar(client, auth)

    respuesta = client.get(f"/api/v1/comprobantes/{creado['id']}/pdf", headers=auth)

    assert respuesta.status_code == 200
    assert not _pdfs_archivados(uow)


def test_si_r2_falla_el_pdf_se_descarga_igual(
    cliente_r2: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    almacenamiento,
    monkeypatch,
) -> None:
    from app.core.errors import StorageError

    creado = _importar_r2(cliente_r2, auth)

    async def _falla(*_a, **_k):
        raise StorageError("R2 caído")

    monkeypatch.setattr(almacenamiento, "upload_file", _falla)

    respuesta = cliente_r2.get(f"/api/v1/comprobantes/{creado['id']}/pdf", headers=auth)

    assert respuesta.status_code == 200
    assert respuesta.content.startswith(b"%PDF")
    assert not _pdfs_archivados(uow)
