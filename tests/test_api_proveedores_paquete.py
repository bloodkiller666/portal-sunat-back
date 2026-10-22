"""Importación de compras desde un ZIP por carpetas: XML + PDF + CDR de cada proveedor."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.enums import EstadoComprobante, OrigenComprobante, SunatAmbiente
from app.services.proveedores_paquete import Entrada, agrupar
from tests.conftest import RUC_PRINCIPAL, crear_token
from tests.fakes import FakeUnitOfWork, construir_comprobante

UBL = Path(__file__).parent / "fixtures" / "ubl"
RUTA = f"/api/v1/companies/{RUC_PRINCIPAL}/comprobantes/import-proveedores"
PDF = b"%PDF-1.4\nfactura del proveedor\n%%EOF\n"

# `factura_recibida.xml`: F010-00000456 del proveedor 20999999999 a la empresa 20123456789.
FACTURA = (UBL / "factura_recibida.xml").read_bytes()
CARPETA = "20999999999/F010-00000456"


def _cdr(referencia: str = "F010-00000456", codigo: str = "0") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ar:ApplicationResponse xmlns:ar="urn:oasis:names:specification:ubl:schema:xsd:'
        'ApplicationResponse-2" xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:'
        'CommonAggregateComponents-2" xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:'
        'CommonBasicComponents-2"><cbc:ID>abc</cbc:ID><cbc:ResponseDate>2026-10-01</cbc:ResponseDate>'
        f"<cac:DocumentResponse><cac:Response><cbc:ReferenceID>{referencia}</cbc:ReferenceID>"
        f"<cbc:ResponseCode>{codigo}</cbc:ResponseCode>"
        f"<cbc:Description>La Factura {referencia} ha sido aceptada</cbc:Description>"
        "</cac:Response></cac:DocumentResponse></ar:ApplicationResponse>"
    ).encode()


GUIA = b'<DespatchAdvice xmlns="urn:oasis:names:specification:ubl:schema:xsd:DespatchAdvice-2"/>'


def _zip(*archivos: tuple[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for nombre, contenido in archivos:
            zf.writestr(nombre, contenido)
    return buffer.getvalue()


def _subir(client: TestClient, headers: dict[str, str], contenido: bytes):
    return client.post(
        RUTA, headers=headers, files={"paquete": ("proveedores.zip", contenido, "application/zip")}
    )


# --------------------------------------------------------------------------- #
# Agrupación (pura)
# --------------------------------------------------------------------------- #
def test_con_un_solo_comprobante_la_carpeta_entera_es_suya() -> None:
    agrupacion = agrupar(
        [
            Entrada(f"{CARPETA}/cualquier-nombre.xml", FACTURA),
            Entrada(f"{CARPETA}/documento-1.pdf", PDF),
            Entrada(f"{CARPETA}/otro.pdf", PDF),
            Entrada(f"{CARPETA}/R-cdr.xml", _cdr()),
        ]
    )

    (asignacion,) = agrupacion.asignaciones
    assert [p.nombre for p in asignacion.pdfs] == ["documento-1.pdf", "otro.pdf"]
    assert asignacion.cdr is not None and asignacion.cdr.nombre == "R-cdr.xml"
    assert agrupacion.ignorados == []


def test_la_guia_y_los_archivos_de_otro_tipo_se_ignoran_y_se_informan() -> None:
    agrupacion = agrupar(
        [
            Entrada(f"{CARPETA}/f.xml", FACTURA),
            Entrada(f"{CARPETA}/guia.xml", GUIA),
            Entrada(f"{CARPETA}/hoja.xlsx", b"x"),
            Entrada(f"{CARPETA}/raro.xml", b"<html/>"),
        ]
    )

    assert len(agrupacion.asignaciones) == 1
    assert sorted(codigo for _, codigo, _ in agrupacion.ignorados) == [
        "guia",
        "tipo_no_soportado",
        "xml_no_reconocido",
    ]


def test_en_un_zip_plano_el_pdf_se_une_por_su_nombre_o_por_la_serie_numero() -> None:
    otra = FACTURA.replace(b"F010-00000456", b"F010-00000457")
    agrupacion = agrupar(
        [
            Entrada("a.xml", FACTURA),
            Entrada("b.xml", otra),
            Entrada("a.pdf", PDF),  # mismo nombre que el XML
            Entrada("Factura F010-457.pdf", PDF),  # la serie-número va en el nombre
            Entrada("suelto.pdf", PDF),  # no se puede unir a nadie
            Entrada("R-1.xml", _cdr("F010-00000457")),
        ]
    )

    por_xml = {a.xml.nombre: a for a in agrupacion.asignaciones}
    assert [p.nombre for p in por_xml["a.xml"].pdfs] == ["a.pdf"]
    assert [p.nombre for p in por_xml["b.xml"].pdfs] == ["Factura F010-457.pdf"]
    assert por_xml["b.xml"].cdr is not None and por_xml["a.xml"].cdr is None
    assert [(e.nombre, c) for e, c, _ in agrupacion.ignorados] == [("suelto.pdf", "sin_xml")]


def test_dos_carpetas_con_el_mismo_nombre_de_pdf_no_se_pisan() -> None:
    otra = FACTURA.replace(b"F010-00000456", b"F010-00000457")
    agrupacion = agrupar(
        [
            Entrada("20999999999/F010-00000456/factura.xml", FACTURA),
            Entrada("20999999999/F010-00000456/factura.pdf", PDF + b"1"),
            Entrada("20999999999/F010-00000457/factura.xml", otra),
            Entrada("20999999999/F010-00000457/factura.pdf", PDF + b"2"),
        ]
    )

    assert {a.pdfs[0].contenido for a in agrupacion.asignaciones} == {PDF + b"1", PDF + b"2"}


# --------------------------------------------------------------------------- #
# Endpoint
# --------------------------------------------------------------------------- #
def test_registra_la_compra_con_su_pdf_y_su_cdr(
    cliente_r2: TestClient, uow: FakeUnitOfWork
) -> None:
    paquete = _zip(
        (f"{CARPETA}/factura.xml", FACTURA),
        (f"{CARPETA}/representacion.pdf", PDF),
        (f"{CARPETA}/R-20999999999-01-F010-00000456.xml", _cdr()),
    )

    cuerpo = _subir(cliente_r2, {"Authorization": f"Bearer {crear_token()}"}, paquete).json()

    assert (cuerpo["importados"], cuerpo["con_pdf"], cuerpo["con_cdr"]) == (1, 1, 1)
    (comprobante,) = uow.comprobantes.comprobantes.values()
    assert comprobante.origen is OrigenComprobante.recibido
    assert comprobante.ruc_emisor == "20999999999"
    assert comprobante.cdr_codigo == "0"
    assert {a.tipo.value for a in uow.archivos.archivos} == {"xml", "pdf", "cdr"}


def test_repetir_el_paquete_no_duplica_y_completa_lo_que_falte(
    cliente_r2: TestClient, uow: FakeUnitOfWork
) -> None:
    headers = {"Authorization": f"Bearer {crear_token()}"}
    _subir(cliente_r2, headers, _zip((f"{CARPETA}/factura.xml", FACTURA)))

    segunda = _subir(
        cliente_r2,
        headers,
        _zip(
            (f"{CARPETA}/factura.xml", FACTURA),
            (f"{CARPETA}/representacion.pdf", PDF),
            (f"{CARPETA}/cdr.xml", _cdr()),
        ),
    ).json()
    tercera = _subir(
        cliente_r2,
        headers,
        _zip((f"{CARPETA}/factura.xml", FACTURA), (f"{CARPETA}/representacion.pdf", PDF)),
    ).json()

    assert len(uow.comprobantes.comprobantes) == 1
    assert (segunda["duplicados"], segunda["con_pdf"], segunda["con_cdr"]) == (1, 1, 1)
    assert tercera["duplicados"] == 1
    assert sum(1 for a in uow.archivos.archivos if a.tipo.value == "pdf") == 1  # no se repite
    assert sum(1 for a in uow.archivos.archivos if a.tipo.value == "cdr") == 1


def test_una_compra_de_sire_se_completa_con_el_xml_aunque_el_portal_corra_en_beta(
    cliente_r2: TestClient, uow: FakeUnitOfWork
) -> None:
    empresa = uow.companies.empresas[0]
    sire = construir_comprobante(
        empresa.id,
        ruc="20999999999",
        serie="F010",
        correlativo=456,
        origen=OrigenComprobante.recibido,
        ambiente=SunatAmbiente.prod,
        estado=EstadoComprobante.aprobado,
    ).model_copy(update={"fuente": "sire"})
    uow.comprobantes.comprobantes[sire.id] = sire

    cuerpo = _subir(
        cliente_r2,
        {"Authorization": f"Bearer {crear_token()}"},
        _zip((f"{CARPETA}/f.xml", FACTURA), (f"{CARPETA}/f.pdf", PDF)),
    ).json()

    assert len(uow.comprobantes.comprobantes) == 1  # no se creó una fila aparte en beta
    assert (cuerpo["importados"], cuerpo["con_pdf"], cuerpo["con_error"]) == (1, 1, 0)
    assert uow.comprobantes.comprobantes[sire.id].fuente == "xml"


def test_un_xml_de_otra_empresa_se_informa_como_error(
    cliente_r2: TestClient, uow: FakeUnitOfWork
) -> None:
    ajeno = FACTURA.replace(b"20123456789", b"20555555555")

    cuerpo = _subir(
        cliente_r2, {"Authorization": f"Bearer {crear_token()}"}, _zip((f"{CARPETA}/f.xml", ajeno))
    ).json()

    assert (cuerpo["importados"], cuerpo["con_error"]) == (0, 1)
    assert uow.comprobantes.comprobantes == {}


def test_sin_r2_se_registra_la_compra_aunque_no_se_archive_nada(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork
) -> None:
    cuerpo = _subir(
        client, auth, _zip((f"{CARPETA}/f.xml", FACTURA), (f"{CARPETA}/f.pdf", PDF))
    ).json()

    assert (cuerpo["importados"], cuerpo["con_pdf"]) == (1, 0)
    assert uow.archivos.archivos == []


def test_exige_un_zip_con_algun_xml(client: TestClient, auth: dict[str, str]) -> None:
    assert _subir(client, auth, b"esto no es un zip").status_code == 422
    assert _subir(client, auth, _zip(("a.pdf", PDF))).status_code == 422


def test_un_lector_no_puede_importar(client: TestClient, auth_lector: dict[str, str]) -> None:
    assert _subir(client, auth_lector, _zip((f"{CARPETA}/f.xml", FACTURA))).status_code == 403


def test_el_estado_no_se_degrada_con_el_cdr(cliente_r2: TestClient, uow: FakeUnitOfWork) -> None:
    paquete = _zip((f"{CARPETA}/f.xml", FACTURA), (f"{CARPETA}/cdr.xml", _cdr(codigo="0")))

    _subir(cliente_r2, {"Authorization": f"Bearer {crear_token()}"}, paquete)

    (comprobante,) = uow.comprobantes.comprobantes.values()
    assert comprobante.estado is EstadoComprobante.aprobado
