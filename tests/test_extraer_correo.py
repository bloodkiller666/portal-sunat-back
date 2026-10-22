"""Extractor de adjuntos de Outlook: la parte pura (nombres, ZIP seguros y rutas), sin Outlook."""

from __future__ import annotations

import io
import zipfile
from datetime import datetime
from pathlib import Path

from scripts import extraer_correo_outlook as ex


def _zip(entradas: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for nombre, datos in entradas.items():
            zf.writestr(nombre, datos)
    return buffer.getvalue()


def test_el_nombre_nunca_lleva_rutas_ni_caracteres_reservados() -> None:
    assert ex.nombre_seguro("..\\..\\Windows\\sistema.xml") == "sistema.xml"
    assert ex.nombre_seguro("a:b*c?.pdf") == "a-b-c-.pdf"
    assert ex.nombre_seguro("  ..  ", "x") == "x"
    assert len(ex.nombre_seguro("a" * 400 + ".pdf")) <= 150


def test_la_carpeta_del_correo_agrupa_por_mes_y_no_usa_el_asunto(tmp_path: Path) -> None:
    ruta = ex.carpeta_de_correo(tmp_path, datetime(2026, 10, 21, 16, 6, 5), "abcdef123456")

    assert ruta == tmp_path / "2026-10" / "20261021-160605_abcdef"


def test_un_nombre_repetido_no_pisa_el_archivo_existente(tmp_path: Path) -> None:
    (tmp_path / "f.pdf").write_bytes(b"1")
    (tmp_path / "f (2).pdf").write_bytes(b"2")

    assert ex.nombre_libre(tmp_path, "f.pdf") == tmp_path / "f (3).pdf"
    assert ex.nombre_libre(tmp_path, "g.pdf") == tmp_path / "g.pdf"


def test_del_zip_solo_salen_xml_y_pdf_con_su_nombre_base() -> None:
    contenido = _zip(
        {
            "20123456789-01-F001-1.xml": b"<xml/>",
            "sub/carpeta/R-20123456789-01-F001-1.xml": b"<cdr/>",
            "F001-1.pdf": b"%PDF",
            "leeme.txt": b"hola",
        }
    )

    archivos, omitidos = ex.leer_zip(contenido)

    assert [nombre for nombre, _ in archivos] == [
        "20123456789-01-F001-1.xml",
        "R-20123456789-01-F001-1.xml",
        "F001-1.pdf",
    ]
    assert omitidos == ["zip_omite_txt"]


def test_una_entrada_con_ruta_peligrosa_se_guarda_solo_con_su_nombre() -> None:
    archivos, _ = ex.leer_zip(_zip({"../../afuera.xml": b"<x/>"}))

    assert [nombre for nombre, _ in archivos] == ["afuera.xml"]


def test_un_zip_invalido_se_informa_y_no_detiene_nada() -> None:
    assert ex.leer_zip(b"esto no es un zip") == ([], ["zip_invalido"])


def test_un_zip_con_demasiados_archivos_se_rechaza(monkeypatch) -> None:
    monkeypatch.setattr(ex, "MAX_ENTRADAS_ZIP", 2)

    archivos, omitidos = ex.leer_zip(_zip({f"{i}.xml": b"x" for i in range(3)}))

    assert archivos == []
    assert omitidos == ["zip_con_demasiados_archivos"]


def test_un_archivo_del_zip_demasiado_grande_se_omite(monkeypatch) -> None:
    monkeypatch.setattr(ex, "MAX_BYTES_ENTRADA", 10)

    archivos, omitidos = ex.leer_zip(_zip({"a.xml": b"x" * 50}))

    assert archivos == []
    assert omitidos == ["zip_supera_el_tamano_maximo"]


def test_el_id_del_correo_es_estable() -> None:
    assert ex.id_correo("ENTRY-1") == ex.id_correo("ENTRY-1")
    assert ex.id_correo("ENTRY-1") != ex.id_correo("ENTRY-2")


# --------------------------------------------------------------------------- #
# Organización AAAA/MM/RUC/SERIE-NÚMERO
# --------------------------------------------------------------------------- #
UBL = Path(__file__).parent / "fixtures" / "ubl"
FACTURA = (UBL / "factura_recibida.xml").read_bytes()  # F010-00000456 de 20999999999 a 20123456789
EMPRESA = "20123456789"
RECIBIDO = datetime(2026, 10, 21, 16, 6, 5)
PDF = b"%PDF-1.4 factura"
GUIA = b'<DespatchAdvice xmlns="urn:oasis:names:specification:ubl:schema:xsd:DespatchAdvice-2"/>'
CARPETA = "2026/10/20999999999/F010-00000456"


def _cdr(referencia: str = "F010-00000456") -> bytes:
    return (
        '<ar:ApplicationResponse xmlns:ar="urn:oasis:names:specification:ubl:schema:xsd:'
        'ApplicationResponse-2" xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:'
        'CommonAggregateComponents-2" xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:'
        'CommonBasicComponents-2"><cac:DocumentResponse><cac:Response>'
        f"<cbc:ReferenceID>{referencia}</cbc:ReferenceID><cbc:ResponseCode>0</cbc:ResponseCode>"
        "</cac:Response></cac:DocumentResponse></ar:ApplicationResponse>"
    ).encode()


def _plan(*archivos: tuple[str, bytes]):
    return ex.planificar_correo(
        [(n, d, "adjunto") for n, d in archivos],
        ruc_empresa=EMPRESA,
        recibido=RECIBIDO,
        identificador="abcdef123456",
    )


def test_la_carpeta_sale_del_xml_y_no_de_la_fecha_del_correo() -> None:
    assert (
        ex.carpeta_de_comprobante(
            "20999999999", "F010", 456, datetime(2026, 7, 3), EMPRESA, EMPRESA
        )
        == "2026/07/20999999999/F010-00000456"
    )
    assert ex.carpeta_de_comprobante("20999999999", "F010", 456, None, None, EMPRESA) == (
        "sin-fecha/sin-fecha/20999999999/F010-00000456"
    )


def test_una_factura_a_otro_ruc_no_se_mezcla_con_las_de_la_empresa() -> None:
    carpeta = ex.carpeta_de_comprobante(
        "20999999999", "F010", 456, datetime(2026, 7, 3), "2061", EMPRESA
    )

    assert carpeta == "_otras_empresas/2061/2026/07/20999999999/F010-00000456"


def test_el_xml_el_pdf_y_el_cdr_del_correo_quedan_juntos() -> None:
    plan = _plan(
        ("factura.xml", FACTURA),
        ("cualquier nombre.pdf", PDF),
        ("R-cdr.xml", _cdr()),
        ("guia.xml", GUIA),
    )

    assert sorted(c.ruta for c in plan.colocaciones) == sorted(
        f"{CARPETA}/{n}" for n in ("factura.xml", "cualquier nombre.pdf", "R-cdr.xml", "guia.xml")
    )
    (colocado,) = plan.comprobantes
    assert (colocado.serie, colocado.correlativo, colocado.empresa) == ("F010", 456, True)
    assert plan.sin_clasificar == []


def test_un_pdf_sin_xml_se_aparta_porque_no_dice_a_que_documento_pertenece() -> None:
    plan = _plan(("solo.pdf", PDF))

    assert [c.ruta for c in plan.colocaciones] == [
        "_sin_clasificar/2026-10/20261021-160605_abcdef/solo.pdf"
    ]
    assert plan.comprobantes == []
    assert plan.sin_clasificar == [("solo.pdf", "sin_xml")]


def test_dos_adjuntos_con_el_mismo_nombre_en_un_correo_no_se_pisan() -> None:
    plan = _plan(("f.pdf", PDF + b"1"), ("f.pdf", PDF + b"2"))

    assert len({c.ruta for c in plan.colocaciones}) == 2


def test_un_xml_de_otro_ruc_va_a_otras_empresas() -> None:
    ajena = FACTURA.replace(b"20123456789", b"20555555555")

    plan = _plan(("f.xml", ajena))

    assert plan.colocaciones[0].ruta.startswith("_otras_empresas/20555555555/2026/10/")
    assert plan.comprobantes[0].empresa is False


def test_un_archivo_identico_no_se_guarda_dos_veces(tmp_path: Path) -> None:
    assert ex.guardar_sin_duplicar(tmp_path / "c", "f.pdf", PDF) is True
    assert ex.guardar_sin_duplicar(tmp_path / "c", "otro-nombre.pdf", PDF) is False
    assert ex.guardar_sin_duplicar(tmp_path / "c", "f.pdf", PDF + b"distinto") is True
    assert sorted(p.name for p in (tmp_path / "c").iterdir()) == ["f (2).pdf", "f.pdf"]


def test_el_cdr_que_llego_en_otro_correo_se_une_a_su_factura(tmp_path: Path) -> None:
    carpeta = tmp_path / CARPETA
    carpeta.mkdir(parents=True)
    (carpeta / "f.xml").write_bytes(FACTURA)
    suelto = tmp_path / "_sin_clasificar" / "2026-10" / "x"
    suelto.mkdir(parents=True)
    (suelto / "R-1.xml").write_bytes(_cdr())
    (suelto / "otro.xml").write_bytes(_cdr("F999-00000001"))  # de una factura que no tenemos
    indice = {
        ("F010", 456): [{"serie": "F010", "correlativo": 456, "carpeta": CARPETA, "empresa": True}]
    }

    unidos = ex.unir_sueltos(tmp_path, indice)

    assert unidos == {"cdr": 1}
    assert (carpeta / "R-1.xml").exists()
    assert (suelto / "otro.xml").exists()


def test_no_se_une_si_la_serie_numero_es_ambigua_o_ya_hay_cdr(tmp_path: Path) -> None:
    for ruc in ("20111111111", "20222222222"):
        (tmp_path / "2026/10" / ruc / "F010-00000456").mkdir(parents=True)
    suelto = tmp_path / "_sin_clasificar" / "x"
    suelto.mkdir(parents=True)
    (suelto / "R-1.xml").write_bytes(_cdr())
    dos = [
        {
            "serie": "F010",
            "correlativo": 456,
            "carpeta": f"2026/10/{r}/F010-00000456",
            "empresa": True,
        }
        for r in ("20111111111", "20222222222")
    ]

    assert ex.unir_sueltos(tmp_path, {("F010", 456): dos}) == {}
    assert (suelto / "R-1.xml").exists()


def test_los_lotes_no_parten_una_carpeta_y_respetan_los_topes() -> None:
    carpetas = [(f"c{i}", [("a.xml", 10), ("a.pdf", 10), ("r.xml", 10)]) for i in range(5)]

    assert ex.trozos_para_lotes(carpetas, max_archivos=7, max_bytes=10_000) == [
        ["c0", "c1"],
        ["c2", "c3"],
        ["c4"],
    ]
    assert ex.trozos_para_lotes(carpetas, max_archivos=100, max_bytes=65) == [
        ["c0", "c1"],
        ["c2", "c3"],
        ["c4"],
    ]
    assert ex.trozos_para_lotes([], max_archivos=1, max_bytes=1) == []


def test_los_zip_de_importacion_llevan_cada_compra_en_su_carpeta(tmp_path: Path) -> None:
    carpeta = tmp_path / CARPETA
    carpeta.mkdir(parents=True)
    (carpeta / "f.xml").write_bytes(FACTURA)
    (carpeta / "f.pdf").write_bytes(PDF)
    ajena = tmp_path / "_otras_empresas/2055/2026/10/20999999999/F010-00000456"
    ajena.mkdir(parents=True)
    (ajena / "f.xml").write_bytes(FACTURA)

    (zip_,) = ex.crear_paquetes(tmp_path, simular=False)

    with zipfile.ZipFile(zip_) as zf:
        assert sorted(zf.namelist()) == [f"{CARPETA}/f.pdf", f"{CARPETA}/f.xml"]
    assert ex.crear_paquetes(tmp_path, simular=True) == []


def test_el_zip_original_se_conserva_aparte_y_no_cuenta_como_sin_clasificar() -> None:
    plan = ex.planificar_correo(
        [("f.xml", FACTURA, "zip:lote.zip"), ("lote.zip", b"PK", "zip_original")],
        ruc_empresa=EMPRESA,
        recibido=RECIBIDO,
        identificador="abcdef123456",
    )

    rutas = {c.ruta for c in plan.colocaciones}
    assert f"{CARPETA}/f.xml" in rutas
    assert "_originales_zip/2026-10/20261021-160605_abcdef/lote.zip" in rutas
    assert plan.sin_clasificar == []


def test_un_rar_o_un_archivo_sin_extension_se_aparta_para_revisarlo_a_mano() -> None:
    plan = _plan(("comprimido.rar", b"Rar!"))

    assert plan.colocaciones[0].ruta.startswith("_sin_clasificar/")
    assert plan.sin_clasificar == [("comprimido.rar", "revisar_a_mano")]
