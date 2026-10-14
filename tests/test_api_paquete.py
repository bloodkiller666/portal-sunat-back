"""Importación masiva de un paquete ZIP (XML + PDF emparejados)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.enums import OrigenComprobante
from app.db.models import Company
from tests.conftest import RUC_PRINCIPAL
from tests.fakes import FakeUnitOfWork, resultado_con_cdr  # noqa: F401

UBL = Path(__file__).parent / "fixtures" / "ubl"
RUTA = f"/api/v1/companies/{RUC_PRINCIPAL}/comprobantes/import-paquete"
PDF = b"%PDF-1.4\nrepresentacion impresa\n%%EOF\n"


def _leer(nombre: str) -> bytes:
    return (UBL / nombre).read_bytes()


def _paquete(*archivos: tuple[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zip_:
        for nombre, contenido in archivos:
            zip_.writestr(nombre, contenido)
    return buffer.getvalue()


def _subir(client: TestClient, auth: dict[str, str], contenido: bytes, **datos: str):
    return client.post(
        RUTA,
        headers=auth,
        data=datos,
        files={"paquete": ("comprobantes.zip", contenido, "application/zip")},
    )


def test_sin_token_devuelve_401(client: TestClient) -> None:
    assert client.post(RUTA, files={"paquete": ("p.zip", b"PK")}).status_code == 401


def test_como_lector_devuelve_403(client: TestClient, auth_lector: dict[str, str]) -> None:
    respuesta = _subir(client, auth_lector, _paquete(("a.xml", _leer("factura.xml"))))
    assert respuesta.status_code == 403


def test_importa_los_xml_del_paquete(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork
) -> None:
    paquete = _paquete(
        ("lote/factura.xml", _leer("factura.xml")),
        ("lote/boleta.xml", _leer("boleta.xml")),
    )

    respuesta = _subir(client, auth, paquete)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["total"] == 2
    assert cuerpo["importados"] == 2
    assert cuerpo["duplicados"] == 0
    assert cuerpo["con_error"] == 0
    assert {item["numero"] for item in cuerpo["items"]} == {
        f"{RUC_PRINCIPAL}-01-F001-00000001",
        f"{RUC_PRINCIPAL}-03-B001-00000005",
    }
    assert len(uow.comprobantes.comprobantes) == 2


def test_los_errores_no_detienen_el_resto(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork
) -> None:
    paquete = _paquete(
        ("factura.xml", _leer("factura.xml")),
        ("roto.xml", b"<Invoice"),  # inválido
        ("otro_ruc.xml", _leer("factura.xml").replace(b"20123456789", b"20111111111")),
        ("factura_copia.xml", _leer("factura.xml")),  # duplicado
    )

    cuerpo = _subir(client, auth, paquete).json()

    assert cuerpo["importados"] == 1
    assert cuerpo["duplicados"] == 1
    assert cuerpo["con_error"] == 2  # el XML truncado y el de otro RUC
    assert cuerpo["sin_xml"] == 0
    # Las cifras particionan el total: nada se cuenta dos veces.
    assert (
        cuerpo["importados"] + cuerpo["duplicados"] + cuerpo["con_error"] + cuerpo["sin_xml"]
        == cuerpo["total"]
        == 4
    )
    codigos = {item["code"] for item in cuerpo["items"] if not item["ok"]}
    assert codigos == {"conflict", "invalid_document"}
    assert len(uow.comprobantes.comprobantes) == 1


def test_archiva_el_pdf_que_acompana_al_xml(
    cliente_r2: TestClient, uow: FakeUnitOfWork
) -> None:
    from tests.conftest import crear_token

    paquete = _paquete(
        ("20123456789-01-F001-00000001.xml", _leer("factura.xml")),
        ("20123456789-01-F001-00000001.pdf", PDF),
    )

    cuerpo = _subir(cliente_r2, {"Authorization": f"Bearer {crear_token()}"}, paquete).json()

    assert cuerpo["importados"] == 1
    assert cuerpo["con_pdf"] == 1
    pdf = next(a for a in uow.archivos.archivos if a.tipo.value == "pdf")
    assert pdf.mime_type == "application/pdf"
    assert pdf.nombre == "20123456789-01-F001-00000001.pdf"
    xml = next(a for a in uow.archivos.archivos if a.tipo.value == "xml")
    assert xml.comprobante_id == pdf.comprobante_id


def test_sin_r2_la_importacion_masiva_sigue_funcionando(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork
) -> None:
    paquete = _paquete(
        ("factura.xml", _leer("factura.xml")),
        ("factura.pdf", PDF),
    )

    cuerpo = _subir(client, auth, paquete).json()

    assert cuerpo["importados"] == 1
    assert cuerpo["con_pdf"] == 0  # no hay bucket: el PDF no se archiva
    assert uow.archivos.archivos == []


def test_pdf_sin_xml_se_informa(client: TestClient, auth: dict[str, str]) -> None:
    paquete = _paquete(
        ("factura.xml", _leer("factura.xml")),
        ("suelto.pdf", PDF),
    )

    cuerpo = _subir(client, auth, paquete).json()

    suelto = next(item for item in cuerpo["items"] if item["archivo"] == "suelto.pdf")
    assert suelto["ok"] is False
    assert suelto["code"] == "sin_xml"
    assert cuerpo["con_error"] == 0  # no es un error del comprobante
    assert cuerpo["sin_xml"] == 1
    assert cuerpo["importados"] == 1


def test_paquete_sin_xml(client: TestClient, auth: dict[str, str]) -> None:
    respuesta = _subir(client, auth, _paquete(("factura.pdf", PDF)))
    assert respuesta.status_code == 422
    assert "XML" in respuesta.json()["error"]["message"]


def test_paquete_corrupto(client: TestClient, auth: dict[str, str]) -> None:
    respuesta = _subir(client, auth, b"PK\x03\x04esto no es un zip")
    assert respuesta.status_code == 422


def test_paquete_que_no_es_zip(client: TestClient, auth: dict[str, str]) -> None:
    respuesta = _subir(client, auth, b"no soy un zip")
    assert respuesta.status_code == 422


def test_demasiados_archivos_en_el_paquete(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.paquetes.MAX_ARCHIVOS_PAQUETE", 1)
    paquete = _paquete(("a.xml", _leer("factura.xml")), ("b.xml", _leer("boleta.xml")))

    respuesta = _subir(client, auth, paquete)

    assert respuesta.status_code == 413
    assert respuesta.json()["error"]["details"]["maximo_archivos"] == 1


def test_importa_compras_con_origen_recibido(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork
) -> None:
    paquete = _paquete(("compra.xml", _leer("factura_recibida.xml")))

    cuerpo = _subir(client, auth, paquete, origen="recibido").json()

    assert cuerpo["importados"] == 1
    assert cuerpo["origen"] == "recibido"
    guardado = next(iter(uow.comprobantes.comprobantes.values()))
    assert guardado.origen is OrigenComprobante.recibido


def test_los_pdf_de_una_compra_tambien_se_archivan(
    cliente_r2: TestClient, uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    from tests.conftest import crear_token

    paquete = _paquete(
        ("compra.xml", _leer("factura_recibida.xml")),
        ("compra.pdf", PDF),
    )

    cuerpo = _subir(
        cliente_r2, {"Authorization": f"Bearer {crear_token()}"}, paquete, origen="recibido"
    ).json()

    assert cuerpo["con_pdf"] == 1
    assert empresa_principal.ruc == RUC_PRINCIPAL
