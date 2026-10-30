"""Importación de compras desde un ZIP por carpetas: XML + PDF de cada proveedor.

El CDR ya no se guarda: si llega, se ignora y se informa."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from uuid import uuid4

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
    # El CDR de la carpeta con una sola factura es de esa factura.
    assert [c.nombre for c in asignacion.cdrs] == ["R-cdr.xml"]
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
    # El CDR cita F010-00000457: va con la segunda factura.
    assert [c.nombre for c in por_xml["b.xml"].cdrs] == ["R-1.xml"]
    assert por_xml["a.xml"].cdrs == []
    assert [(e.nombre, c) for e, c, _ in agrupacion.ignorados] == [("suelto.pdf", "sin_xml")]


def test_un_cdr_que_no_cita_ninguna_factura_de_la_carpeta_se_ignora() -> None:
    otra = FACTURA.replace(b"F010-00000456", b"F010-00000457")
    agrupacion = agrupar(
        [
            Entrada("a.xml", FACTURA),
            Entrada("b.xml", otra),
            Entrada("R-1.xml", _cdr("F010-00000999")),
        ]
    )

    assert [(e.nombre, c) for e, c, _ in agrupacion.ignorados] == [("R-1.xml", "cdr_ignorado")]
    assert all(a.cdrs == [] for a in agrupacion.asignaciones)


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

    assert (cuerpo["importados"], cuerpo["con_pdf"], cuerpo["con_error"]) == (1, 1, 0)
    assert cuerpo["con_cdr"] == 1
    (comprobante,) = uow.comprobantes.comprobantes.values()
    assert comprobante.origen is OrigenComprobante.recibido
    assert comprobante.ruc_emisor == "20999999999"
    assert comprobante.cdr_codigo is None  # un CDR recibido no toca el estado ni el código
    assert {a.tipo.value for a in uow.archivos.archivos} == {"xml", "pdf", "cdr"}
    (cdr_guardado,) = [a for a in uow.archivos.archivos if a.tipo.value == "cdr"]
    assert cdr_guardado.mime_type == "application/xml"  # es un XML suelto, no el ZIP de SUNAT
    (cdr,) = [i for i in cuerpo["items"] if i["archivo"].startswith("R-")]
    assert (cdr["ok"], cdr["tipo"], cdr["situacion"]) == (True, "cdr", "guardado")


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
    assert (segunda["duplicados"], segunda["con_pdf"], segunda["con_error"]) == (1, 1, 0)
    assert tercera["duplicados"] == 1
    assert sum(1 for a in uow.archivos.archivos if a.tipo.value == "pdf") == 1  # no se repite
    assert sum(1 for a in uow.archivos.archivos if a.tipo.value == "cdr") == 1  # tampoco


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
    assert _subir(client, auth, _zip(("a.txt", b"hola"))).status_code == 422


def test_un_lector_no_puede_importar(client: TestClient, auth_lector: dict[str, str]) -> None:
    assert _subir(client, auth_lector, _zip((f"{CARPETA}/f.xml", FACTURA))).status_code == 403


def test_un_cdr_rechazado_no_cambia_el_estado_de_la_compra(
    cliente_r2: TestClient, uow: FakeUnitOfWork
) -> None:
    """El CDR se ignora: ni siquiera uno de rechazo mueve el estado que trae SIRE."""
    _compra_de_sire(uow)
    paquete = _zip((f"{CARPETA}/f.xml", FACTURA), (f"{CARPETA}/cdr.xml", _cdr(codigo="2335")))

    cuerpo = _subir(cliente_r2, _token(), paquete).json()

    (comprobante,) = uow.comprobantes.comprobantes.values()
    assert comprobante.estado is EstadoComprobante.aprobado
    assert cuerpo["con_error"] == 0


# --------------------------------------------------------------------------- #
# Resultado por archivo, situación y PDF sueltos
# --------------------------------------------------------------------------- #
GRUPO = f"/api/v1/companies/{RUC_PRINCIPAL}/comprobantes/import-proveedores/archivos"


def _pdf_con_texto(texto: str) -> bytes:
    """PDF mínimo de una página con ese texto (lo bastante válido para que pypdf lo lea)."""
    contenido = f"BT /F1 12 Tf 72 720 Td ({texto}) Tj ET".encode()
    objetos = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length "
        + str(len(contenido)).encode()
        + b" >>\nstream\n"
        + contenido
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    salida = bytearray(b"%PDF-1.4\n")
    posiciones = []
    for numero, objeto in enumerate(objetos, start=1):
        posiciones.append(len(salida))
        salida += f"{numero} 0 obj\n".encode() + objeto + b"\nendobj\n"
    inicio_xref = len(salida)
    salida += f"xref\n0 {len(objetos) + 1}\n0000000000 65535 f \n".encode()
    for posicion in posiciones:
        salida += f"{posicion:010d} 00000 n \n".encode()
    salida += (
        f"trailer\n<< /Size {len(objetos) + 1} /Root 1 0 R >>\nstartxref\n{inicio_xref}\n%%EOF\n"
    ).encode()
    return bytes(salida)


def _grupo(client: TestClient, headers: dict[str, str], *archivos: tuple[str, bytes]):
    return client.post(
        GRUPO,
        headers=headers,
        files=[
            ("archivos", (nombre, datos, "application/octet-stream")) for nombre, datos in archivos
        ],
    )


def _compra_de_sire(uow: FakeUnitOfWork, **campos):
    empresa = uow.companies.empresas[0]
    compra = construir_comprobante(
        empresa.id,
        ruc="20999999999",
        serie="F010",
        correlativo=456,
        origen=OrigenComprobante.recibido,
        ambiente=SunatAmbiente.prod,
        estado=EstadoComprobante.aprobado,
    ).model_copy(update={"fuente": "sire", **campos})
    uow.comprobantes.comprobantes[compra.id] = compra
    return compra


def _token() -> dict[str, str]:
    return {"Authorization": f"Bearer {crear_token()}"}


def test_cada_archivo_trae_su_tipo_y_la_situacion_de_la_compra(
    cliente_r2: TestClient, uow: FakeUnitOfWork
) -> None:
    paquete = _zip(
        (f"{CARPETA}/f.xml", FACTURA), (f"{CARPETA}/f.pdf", PDF), (f"{CARPETA}/c.xml", _cdr())
    )

    nueva = _subir(cliente_r2, _token(), paquete).json()
    repetida = _subir(cliente_r2, _token(), paquete).json()

    assert [(i["tipo"], i["situacion"]) for i in nueva["items"]] == [
        ("xml", "nueva"),
        ("pdf", "guardado"),
        ("cdr", "guardado"),  # el CDR llega como .xml y se guarda con su compra
    ]
    assert (nueva["nuevas"], nueva["completadas"], nueva["importados"]) == (1, 0, 1)
    assert [(i["tipo"], i["situacion"]) for i in repetida["items"]] == [
        ("xml", "repetida"),
        ("pdf", "ya_estaba"),
        ("cdr", "ya_estaba"),
    ]
    assert (repetida["duplicados"], repetida["importados"], repetida["con_error"]) == (1, 0, 0)


def test_una_compra_de_sire_queda_como_completada(
    cliente_r2: TestClient, uow: FakeUnitOfWork
) -> None:
    _compra_de_sire(uow)

    cuerpo = _grupo(cliente_r2, _token(), ("f.xml", FACTURA), ("f.pdf", PDF)).json()

    assert cuerpo["items"][0]["situacion"] == "completada"
    assert (cuerpo["completadas"], cuerpo["nuevas"], cuerpo["con_pdf"]) == (1, 0, 1)


def test_el_endpoint_por_grupos_importa_una_compra_con_sus_archivos(
    cliente_r2: TestClient, uow: FakeUnitOfWork
) -> None:
    cuerpo = _grupo(
        cliente_r2,
        _token(),
        ("factura.xml", FACTURA),
        ("representacion.pdf", PDF),
        ("R-cdr.xml", _cdr()),
    ).json()

    assert (cuerpo["total"], cuerpo["importados"]) == (3, 1)
    assert (cuerpo["con_pdf"], cuerpo["con_error"]) == (1, 0)
    assert {i["archivo"] for i in cuerpo["items"]} == {
        "factura.xml",
        "representacion.pdf",
        "R-cdr.xml",
    }
    (comprobante,) = uow.comprobantes.comprobantes.values()
    assert comprobante.ruc_emisor == "20999999999"


def test_un_xml_con_error_marca_sus_pdf_como_no_procesados(
    cliente_r2: TestClient, uow: FakeUnitOfWork
) -> None:
    ajeno = FACTURA.replace(b"20123456789", b"20555555555")

    cuerpo = _grupo(cliente_r2, _token(), ("f.xml", ajeno), ("f.pdf", PDF)).json()

    assert [(i["tipo"], i["ok"], i["code"]) for i in cuerpo["items"]] == [
        ("xml", False, "invalid_document"),
        ("pdf", False, "xml_con_error"),
    ]
    assert cuerpo["con_error"] == 1  # el PDF no cuenta como un error más


def test_un_pdf_suelto_se_une_a_la_compra_que_su_texto_identifica(
    cliente_r2: TestClient, uow: FakeUnitOfWork
) -> None:
    compra = _compra_de_sire(uow)
    pdf = _pdf_con_texto(
        "PROVEEDOR SAC RUC 20999999999 FACTURA ELECTRONICA F010-00000456 total 118"
    )

    cuerpo = _grupo(cliente_r2, _token(), ("scan-001.pdf", pdf)).json()

    (item,) = cuerpo["items"]
    assert (item["tipo"], item["ok"], item["situacion"]) == ("pdf", True, "unido")
    assert item["comprobante_id"] == str(compra.id)
    assert any(
        a.tipo.value == "pdf" and a.comprobante_id == compra.id for a in uow.archivos.archivos
    )


def test_un_pdf_que_no_se_puede_identificar_se_informa_sin_error(
    cliente_r2: TestClient, uow: FakeUnitOfWork
) -> None:
    _compra_de_sire(uow)
    otro_ruc = _pdf_con_texto(
        "PROVEEDOR SAC RUC 20111111111 FACTURA ELECTRONICA F010-00000456 total 1"
    )
    sin_texto = b"%PDF-1.4\n%%EOF\n"

    cuerpo = _grupo(cliente_r2, _token(), ("a.pdf", otro_ruc), ("b.pdf", sin_texto)).json()

    assert [(i["ok"], i["code"], i["situacion"]) for i in cuerpo["items"]] == [
        (False, "sin_xml", "omitido"),
        (False, "sin_xml", "omitido"),
    ]
    assert (cuerpo["con_error"], cuerpo["sin_xml"]) == (0, 2)
    assert uow.archivos.archivos == []


def test_un_pdf_que_coincide_con_dos_compras_no_se_une(
    cliente_r2: TestClient, uow: FakeUnitOfWork
) -> None:
    primera = _compra_de_sire(uow)
    segunda = primera.model_copy(update={"id": uuid4(), "tipo_documento": "07"})
    uow.comprobantes.comprobantes[segunda.id] = segunda
    pdf = _pdf_con_texto("RUC 20999999999 F010-00000456 documento de prueba con suficiente texto")

    cuerpo = _grupo(cliente_r2, _token(), ("x.pdf", pdf)).json()

    assert cuerpo["items"][0]["code"] == "ambiguo"
    assert uow.archivos.archivos == []


def test_el_endpoint_por_grupos_exige_rol_y_archivos(
    client: TestClient, auth: dict[str, str], auth_lector: dict[str, str]
) -> None:
    assert _grupo(client, auth_lector, ("f.xml", FACTURA)).status_code == 403
    assert client.post(GRUPO, headers=auth).status_code == 422


def test_una_compra_con_xml_ya_registrado_no_se_vuelve_a_insertar(
    cliente_r2: TestClient, uow: FakeUnitOfWork, monkeypatch
) -> None:
    """En Postgres un INSERT duplicado aborta la transacción y rompe todo lo que sigue (un 500)."""
    paquete = _zip((f"{CARPETA}/f.xml", FACTURA), (f"{CARPETA}/f.pdf", PDF))
    _subir(cliente_r2, _token(), paquete)
    inserciones: list[object] = []
    original = uow.comprobantes.create

    async def contar(registro):
        inserciones.append(registro)
        return await original(registro)

    monkeypatch.setattr(uow.comprobantes, "create", contar)

    cuerpo = _subir(cliente_r2, _token(), paquete).json()

    assert inserciones == []  # ni siquiera se intentó insertar
    assert [(i["tipo"], i["situacion"]) for i in cuerpo["items"]] == [
        ("xml", "repetida"),
        ("pdf", "ya_estaba"),
    ]


# --------------------------------------------------------------------------- #
# Lo que SIRE manda no siempre cuadra con lo que llega por correo
# --------------------------------------------------------------------------- #
def _fila_sire(uow: FakeUnitOfWork, **campos):
    """Una compra que SIRE ya trajo (sin XML), con la clave que se indique."""
    empresa = uow.companies.empresas[0]
    valores = {
        "ruc": "20999999999",
        "serie": "F010",
        "correlativo": 456,
        "origen": OrigenComprobante.recibido,
        "ambiente": SunatAmbiente.prod,
        "estado": EstadoComprobante.aprobado,
    }
    valores.update(campos)
    fila = construir_comprobante(empresa.id, **valores).model_copy(update={"fuente": "sire"})
    uow.comprobantes.comprobantes[fila.id] = fila
    return fila


def test_si_sire_trae_otro_numero_el_xml_se_registra_como_nueva_y_no_toca_la_de_sire(
    cliente_r2: TestClient, uow: FakeUnitOfWork
) -> None:
    de_sire = _fila_sire(uow, correlativo=457)  # SIRE dice 457; el XML del correo es el 456

    cuerpo = _grupo(cliente_r2, _token(), ("f.xml", FACTURA), ("f.pdf", PDF)).json()

    assert cuerpo["items"][0]["situacion"] == "nueva"
    assert (cuerpo["nuevas"], cuerpo["completadas"]) == (1, 0)
    assert len(uow.comprobantes.comprobantes) == 2
    assert uow.comprobantes.comprobantes[de_sire.id].fuente == "sire"  # sigue sin XML


def test_si_sire_trae_otra_serie_el_xml_no_se_une_a_esa_fila(
    cliente_r2: TestClient, uow: FakeUnitOfWork
) -> None:
    _fila_sire(uow, serie="F011")

    cuerpo = _grupo(cliente_r2, _token(), ("f.xml", FACTURA)).json()

    assert (cuerpo["nuevas"], cuerpo["completadas"]) == (1, 0)


def test_la_misma_serie_numero_de_otro_proveedor_no_se_confunde(
    cliente_r2: TestClient, uow: FakeUnitOfWork
) -> None:
    """Dos proveedores pueden emitir F010-456: la llave incluye el RUC del emisor."""
    ajena = _fila_sire(uow, ruc="20111111111")

    cuerpo = _grupo(cliente_r2, _token(), ("f.xml", FACTURA)).json()

    assert (cuerpo["nuevas"], cuerpo["completadas"]) == (1, 0)
    assert uow.comprobantes.comprobantes[ajena.id].ruc_emisor == "20111111111"
    assert len(uow.comprobantes.comprobantes) == 2


def test_la_misma_serie_numero_con_otro_tipo_de_documento_no_se_confunde(
    cliente_r2: TestClient, uow: FakeUnitOfWork
) -> None:
    _fila_sire(uow, tipo_documento="07")  # una nota de crédito con la misma serie-número

    cuerpo = _grupo(cliente_r2, _token(), ("f.xml", FACTURA)).json()

    assert (cuerpo["nuevas"], cuerpo["completadas"]) == (1, 0)


def test_si_sire_coincide_exacto_el_xml_la_completa(
    cliente_r2: TestClient, uow: FakeUnitOfWork
) -> None:
    de_sire = _fila_sire(uow)

    cuerpo = _grupo(cliente_r2, _token(), ("f.xml", FACTURA)).json()

    assert (cuerpo["nuevas"], cuerpo["completadas"]) == (0, 1)
    assert len(uow.comprobantes.comprobantes) == 1
    assert uow.comprobantes.comprobantes[de_sire.id].fuente == "xml"


def test_una_foto_de_la_factura_no_es_un_error_se_informa_y_el_xml_se_importa(
    cliente_r2: TestClient, uow: FakeUnitOfWork
) -> None:
    foto = b"\xff\xd8\xff\xe0 foto de la factura"

    cuerpo = _grupo(
        cliente_r2, _token(), ("f.xml", FACTURA), ("IMG_0001.jpg", foto), ("guia.png", b"\x89PNG")
    ).json()

    assert (cuerpo["importados"], cuerpo["con_error"]) == (1, 0)
    omitidos = [i for i in cuerpo["items"] if i["situacion"] == "omitido"]
    assert {(i["archivo"], i["code"]) for i in omitidos} == {
        ("IMG_0001.jpg", "tipo_no_soportado"),
        ("guia.png", "tipo_no_soportado"),
    }
    assert not any(a.nombre.endswith((".jpg", ".png")) for a in uow.archivos.archivos)


def test_solo_una_foto_sin_xml_no_crea_nada_ni_da_error(
    cliente_r2: TestClient, uow: FakeUnitOfWork
) -> None:
    cuerpo = _grupo(cliente_r2, _token(), ("IMG_0001.jpg", b"\xff\xd8\xff")).json()

    assert (cuerpo["importados"], cuerpo["con_error"]) == (0, 0)
    assert cuerpo["items"][0]["code"] == "tipo_no_soportado"
    assert uow.comprobantes.comprobantes == {}


def test_un_pdf_escaneado_sin_texto_no_se_une_a_nada_y_no_es_error(
    cliente_r2: TestClient, uow: FakeUnitOfWork
) -> None:
    _fila_sire(uow)
    escaneado = b"%PDF-1.4\n1 0 obj<</Type/XObject/Subtype/Image>>endobj\n%%EOF\n"

    cuerpo = _grupo(cliente_r2, _token(), ("escaneo.pdf", escaneado)).json()

    (item,) = cuerpo["items"]
    assert (item["ok"], item["code"], item["situacion"]) == (False, "sin_xml", "omitido")
    assert cuerpo["con_error"] == 0
    assert uow.archivos.archivos == []


def test_un_pdf_con_otra_serie_numero_que_la_de_sire_no_se_une(
    cliente_r2: TestClient, uow: FakeUnitOfWork
) -> None:
    _fila_sire(uow)  # F010-456
    pdf = _pdf_con_texto("PROVEEDOR SAC RUC 20999999999 FACTURA ELECTRONICA F010-00000999 total 1")

    cuerpo = _grupo(cliente_r2, _token(), ("f.pdf", pdf)).json()

    assert cuerpo["items"][0]["code"] == "sin_xml"
    assert uow.archivos.archivos == []


def test_una_factura_con_sus_guias_de_remision_en_xml_y_pdf_importa_la_factura_sola(
    cliente_r2: TestClient, uow: FakeUnitOfWork
) -> None:
    cuerpo = _grupo(
        cliente_r2, _token(), ("f.xml", FACTURA), ("guia.xml", GUIA), ("guia.pdf", PDF)
    ).json()

    assert (cuerpo["importados"], cuerpo["con_error"]) == (1, 0)
    codigos = {i["archivo"]: i["code"] for i in cuerpo["items"]}
    assert codigos["guia.xml"] == "guia"
    assert len(uow.comprobantes.comprobantes) == 1


def test_un_zip_con_una_compra_buena_y_otra_con_xml_roto_importa_la_buena(
    cliente_r2: TestClient, uow: FakeUnitOfWork
) -> None:
    paquete = _zip(
        (f"{CARPETA}/f.xml", FACTURA),
        ("20111111111/F001-00000001/roto.xml", b"<Invoice><sin cerrar"),
        ("20111111111/F001-00000001/roto.pdf", PDF),
    )

    cuerpo = _subir(cliente_r2, _token(), paquete).json()

    assert cuerpo["importados"] == 1
    assert len(uow.comprobantes.comprobantes) == 1
    roto = [i for i in cuerpo["items"] if i["archivo"] == "roto.xml"]
    assert roto[0]["code"] == "xml_no_reconocido"


def test_el_correo_txt_que_acompana_a_la_carpeta_se_ignora_sin_dar_error(
    cliente_r2: TestClient, uow: FakeUnitOfWork
) -> None:
    paquete = _zip(
        (f"{CARPETA}/f.xml", FACTURA),
        (f"{CARPETA}/f.pdf", PDF),
        (f"{CARPETA}/correo.txt", b"Asunto: Factura F010-456"),
    )

    cuerpo = _subir(cliente_r2, _token(), paquete).json()

    assert (cuerpo["importados"], cuerpo["con_pdf"], cuerpo["con_error"]) == (1, 1, 0)
    texto = [i for i in cuerpo["items"] if i["archivo"] == "correo.txt"]
    assert texto[0]["code"] == "tipo_no_soportado"
