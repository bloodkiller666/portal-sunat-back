"""Extractor de adjuntos de Outlook: la parte pura (nombres, ZIP seguros y rutas), sin Outlook."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
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

    assert ruta == tmp_path / "2026-OCTUBRE" / "20261021-160605_abcdef"


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
            "logo.png": b"x" * 100,  # una imagen diminuta es un logo, no un documento
        }
    )

    archivos, omitidos = ex.leer_zip(contenido)

    assert [nombre for nombre, _ in archivos] == [
        "20123456789-01-F001-1.xml",
        "R-20123456789-01-F001-1.xml",  # el CDR se descarta después, al agrupar
        "F001-1.pdf",
    ]
    assert omitidos == ["zip_omite_txt", "zip_omite_png"]


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
CARPETA = "2026/OCTUBRE/20999999999/F010-00000456"


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


def test_los_meses_van_en_letras_en_todas_las_carpetas() -> None:
    def carpeta(fecha):
        return ex.carpeta_de_comprobante("20999999999", "F010", 1, fecha, EMPRESA, EMPRESA)

    assert carpeta(datetime(2026, 12, 3)) == "2026/DICIEMBRE/20999999999/F010-00000001"
    assert carpeta(datetime(2026, 9, 3)) == "2026/SETIEMBRE/20999999999/F010-00000001"
    assert carpeta(None).startswith("sin-fecha/sin-fecha/")


def test_la_carpeta_sale_del_xml_y_no_de_la_fecha_del_correo() -> None:
    assert (
        ex.carpeta_de_comprobante(
            "20999999999", "F010", 456, datetime(2026, 7, 3), EMPRESA, EMPRESA
        )
        == "2026/JULIO/20999999999/F010-00000456"
    )
    assert ex.carpeta_de_comprobante("20999999999", "F010", 456, None, None, EMPRESA) == (
        "sin-fecha/sin-fecha/20999999999/F010-00000456"
    )


def test_una_factura_a_otro_ruc_no_se_mezcla_con_las_de_la_empresa() -> None:
    carpeta = ex.carpeta_de_comprobante(
        "20999999999", "F010", 456, datetime(2026, 7, 3), "2061", EMPRESA
    )

    assert carpeta == "_otras_empresas/2061/2026/JULIO/20999999999/F010-00000456"


def test_el_xml_el_pdf_y_la_guia_del_correo_quedan_juntos_y_el_cdr_se_descarta() -> None:
    plan = _plan(
        ("factura.xml", FACTURA),
        ("cualquier nombre.pdf", PDF),
        ("R-cdr.xml", _cdr()),
        ("guia.xml", GUIA),
    )

    assert sorted(c.ruta for c in plan.colocaciones) == sorted(
        f"{CARPETA}/{n}" for n in ("factura.xml", "cualquier nombre.pdf", "guia.xml")
    )
    assert plan.omitidos == {"cdr_ignorado": 1}
    (colocado,) = plan.comprobantes
    assert (colocado.serie, colocado.correlativo, colocado.empresa) == ("F010", 456, True)
    assert plan.sin_clasificar == []


def test_un_pdf_sin_xml_se_aparta_porque_no_dice_a_que_documento_pertenece() -> None:
    plan = _plan(("solo.pdf", PDF))

    assert [c.ruta for c in plan.colocaciones] == [
        "_sin_clasificar/2026-OCTUBRE/20261021-160605_abcdef/solo.pdf"
    ]
    assert plan.comprobantes == []
    assert plan.sin_clasificar == [("solo.pdf", "sin_xml")]


def test_dos_adjuntos_con_el_mismo_nombre_en_un_correo_no_se_pisan() -> None:
    plan = _plan(("f.pdf", PDF + b"1"), ("f.pdf", PDF + b"2"))

    assert len({c.ruta for c in plan.colocaciones}) == 2


def test_un_xml_de_otro_ruc_va_a_otras_empresas() -> None:
    ajena = FACTURA.replace(b"20123456789", b"20555555555")

    plan = _plan(("f.xml", ajena))

    assert plan.colocaciones[0].ruta.startswith("_otras_empresas/20555555555/2026/OCTUBRE/")
    assert plan.comprobantes[0].empresa is False


def test_un_archivo_identico_no_se_guarda_dos_veces(tmp_path: Path) -> None:
    assert ex.guardar_sin_duplicar(tmp_path / "c", "f.pdf", PDF) is True
    assert ex.guardar_sin_duplicar(tmp_path / "c", "otro-nombre.pdf", PDF) is False
    assert ex.guardar_sin_duplicar(tmp_path / "c", "f.pdf", PDF + b"distinto") is True
    assert sorted(p.name for p in (tmp_path / "c").iterdir()) == ["f (2).pdf", "f.pdf"]


def _indice(*carpetas: str) -> dict[tuple[str, int], list[dict]]:
    return {
        ("F010", 456): [
            {"serie": "F010", "correlativo": 456, "carpeta": c, "empresa": True} for c in carpetas
        ]
    }


def test_el_pdf_que_llego_en_otro_correo_se_une_a_su_factura(tmp_path: Path) -> None:
    carpeta = tmp_path / CARPETA
    carpeta.mkdir(parents=True)
    (carpeta / "f.xml").write_bytes(FACTURA)
    suelto = tmp_path / "_sin_clasificar" / "2026-OCTUBRE" / "x"
    suelto.mkdir(parents=True)
    (suelto / "Factura F010-456.pdf").write_bytes(PDF)
    (suelto / "F999-1.pdf").write_bytes(PDF)  # de una factura que no tenemos
    (suelto / "R-1.xml").write_bytes(_cdr())  # un CDR suelto ya no se une a nada

    unidos = ex.unir_sueltos(tmp_path, _indice(CARPETA))

    assert unidos == {"pdf": 1}
    assert (carpeta / "Factura F010-456.pdf").exists()
    assert (suelto / "F999-1.pdf").exists()
    assert (suelto / "R-1.xml").exists()
    assert not (carpeta / "R-1.xml").exists()


def test_no_se_une_si_la_serie_numero_es_ambigua_o_ya_hay_pdf(tmp_path: Path) -> None:
    for ruc in ("20111111111", "20222222222"):
        (tmp_path / "2026/OCTUBRE" / ruc / "F010-00000456").mkdir(parents=True)
    suelto = tmp_path / "_sin_clasificar" / "x"
    suelto.mkdir(parents=True)
    (suelto / "F010-456.pdf").write_bytes(PDF)
    ambiguo = _indice(*(f"2026/OCTUBRE/{r}/F010-00000456" for r in ("20111111111", "20222222222")))

    assert ex.unir_sueltos(tmp_path, ambiguo) == {}
    assert (suelto / "F010-456.pdf").exists()

    unica = _indice("2026/OCTUBRE/20111111111/F010-00000456")
    (tmp_path / "2026/OCTUBRE/20111111111/F010-00000456/ya.pdf").write_bytes(b"%PDF otro")
    assert ex.unir_sueltos(tmp_path, unica) == {}  # ya tiene su PDF: ante la duda no se mezcla
    assert (suelto / "F010-456.pdf").exists()


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
    (carpeta / "correo.txt").write_text("evidencia", encoding="utf-8")  # no se importa
    (carpeta / "foto.jpg").write_bytes(b"\xff\xd8")  # tampoco: el importador solo lee XML y PDF
    ajena = tmp_path / "_otras_empresas/2055/2026/OCTUBRE/20999999999/F010-00000456"
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
    assert "_originales_zip/2026-OCTUBRE/20261021-160605_abcdef/lote.zip" in rutas
    assert plan.sin_clasificar == []


def test_un_rar_o_un_archivo_sin_extension_se_aparta_para_revisarlo_a_mano() -> None:
    plan = _plan(("comprimido.rar", b"Rar!"))

    assert plan.colocaciones[0].ruta.startswith("_sin_clasificar/")
    assert plan.sin_clasificar == [("comprimido.rar", "revisar_a_mano")]


# --------------------------------------------------------------------------- #
# Asunto del correo como evidencia (`correo.txt`)
# --------------------------------------------------------------------------- #
META = ex.MetaCorreo(
    asunto="Factura F010-456 octubre",
    remitente="Proveedor SAC <ventas@proveedor.pe>",
    para="recepcionfacturas@holdingtdm.com",
    recibido=RECIBIDO,
    carpeta_outlook="Bandeja de entrada",
    cuerpo="Estimados,\r\nadjunto la factura.\r\n\r\nSaludos",
)


def _plan_con_meta(*archivos: tuple[str, bytes], meta: ex.MetaCorreo | None = META):
    return ex.planificar_correo(
        [(n, d, "adjunto") for n, d in archivos],
        ruc_empresa=EMPRESA,
        recibido=RECIBIDO,
        identificador="abcdef123456",
        meta=meta,
    )


def _texto(plan, carpeta: str) -> str:
    (coloc,) = [c for c in plan.colocaciones if c.ruta == f"{carpeta}/correo.txt"]
    return coloc.datos.decode("utf-8")


def test_cada_carpeta_lleva_el_asunto_el_remitente_y_el_cuerpo_del_correo() -> None:
    plan = _plan_con_meta(("f.xml", FACTURA), ("f.pdf", PDF))

    texto = _texto(plan, CARPETA)

    assert "Asunto:    Factura F010-456 octubre" in texto
    assert "De:        Proveedor SAC <ventas@proveedor.pe>" in texto
    assert "Para:      recepcionfacturas@holdingtdm.com" in texto
    assert "Recibido:  2026-10-21 16:06:05" in texto
    assert "Carpeta:   Bandeja de entrada" in texto
    assert "adjunto la factura." in texto
    assert "\r" not in texto
    assert "  - f.xml (" in texto and "  - f.pdf (" in texto
    assert "sha256 " in texto


def test_sin_meta_no_se_agrega_el_texto_del_correo() -> None:
    plan = _plan_con_meta(("f.xml", FACTURA), meta=None)

    assert not any(c.ruta.endswith("correo.txt") for c in plan.colocaciones)


def test_un_pdf_sin_xml_tambien_lleva_su_correo_para_saber_de_donde_vino() -> None:
    plan = _plan_con_meta(("solo.pdf", PDF))

    carpeta = "_sin_clasificar/2026-OCTUBRE/20261021-160605_abcdef"
    assert "Factura F010-456 octubre" in _texto(plan, carpeta)


def test_el_zip_original_no_lleva_correo_txt_pero_su_contenido_si() -> None:
    plan = ex.planificar_correo(
        [("f.xml", FACTURA, "zip:lote.zip"), ("lote.zip", b"PK", "zip_original")],
        ruc_empresa=EMPRESA,
        recibido=RECIBIDO,
        identificador="abcdef123456",
        meta=META,
    )

    rutas = {c.ruta for c in plan.colocaciones}
    assert f"{CARPETA}/correo.txt" in rutas
    assert not any(r.startswith("_originales_zip/") and r.endswith("correo.txt") for r in rutas)


def test_un_correo_sin_asunto_ni_remitente_igual_deja_constancia() -> None:
    vacio = ex.MetaCorreo("", "", "", RECIBIDO, "MEGA GAS")

    texto = _texto(_plan_con_meta(("f.xml", FACTURA), meta=vacio), CARPETA)

    assert "(sin asunto)" in texto and "(desconocido)" in texto and "(vacío)" in texto


def test_un_cuerpo_enorme_se_recorta(monkeypatch) -> None:
    monkeypatch.setattr(ex, "MAX_CUERPO_CORREO", 50)
    largo = ex.MetaCorreo("a", "b", "c", RECIBIDO, "d", cuerpo="x" * 500)

    texto = ex.texto_correo(largo, [])

    assert "x" * 50 in texto and "x" * 51 not in texto
    assert "[… cuerpo recortado]" in texto


def test_la_misma_factura_en_dos_correos_deja_un_correo_txt_por_cada_uno(tmp_path: Path) -> None:
    informe = ex.Informe()
    segundo = ex.MetaCorreo("Reenvío", "Otro <o@x.pe>", "yo", RECIBIDO, "MEGA GAS")
    tocadas: set[str] = set()

    ex._escribir(tmp_path, _plan_con_meta(("f.xml", FACTURA)), informe, tocadas)
    tocadas.clear()
    reenvio = _plan_con_meta(("f.xml", FACTURA), meta=segundo)
    nuevos = ex._escribir(tmp_path, reenvio, informe, tocadas)

    carpeta = tmp_path / CARPETA
    assert sorted(p.name for p in carpeta.iterdir()) == ["correo (2).txt", "correo.txt", "f.xml"]
    assert nuevos == 1  # el XML idéntico no se duplicó; solo se sumó el segundo correo
    assert tocadas == set()  # un correo.txt nuevo no obliga a importar otra vez la compra


def test_el_correo_txt_no_marca_la_carpeta_como_pendiente_pero_el_xml_si(tmp_path: Path) -> None:
    tocadas: set[str] = set()

    ex._escribir(tmp_path, _plan_con_meta(("f.xml", FACTURA)), ex.Informe(), tocadas)

    assert tocadas == {CARPETA}


# --------------------------------------------------------------------------- #
# Fotos de documentos (factura, guía de remisión… fotografiadas o escaneadas)
# --------------------------------------------------------------------------- #
KB = 1024


@pytest.mark.parametrize(
    ("nombre", "tamano", "incrustada", "esperado"),
    [
        ("IMG_20261021_101500.jpg", 2000 * KB, False, True),  # foto de celular
        ("WhatsApp Image 2026-10-21 at 10.15.00.jpeg", 300 * KB, False, True),
        ("factura.jpg", 10 * KB, False, True),  # el nombre lo dice, aunque pese poco
        ("guia de remision.png", 60 * KB, False, True),
        ("scan0001.tif", 900 * KB, False, True),
        ("image001.png", 20 * KB, False, False),  # firma de Outlook
        ("image002.jpg", 35 * KB, False, False),
        ("image003.png", 3000 * KB, False, True),  # una firma no pesa 3 MB: puede ser un escaneo
        ("logo-empresa.png", 15 * KB, False, False),
        ("banner.gif", 200 * KB, False, False),
        ("facebook.png", 2 * KB, False, False),
        ("foto.png", 100 * KB, True, False),  # pegada en el cuerpo y pequeña
        ("captura.png", 900 * KB, True, True),  # pegada en el cuerpo pero grande
        ("favicon.ico", 900 * KB, False, False),
        ("dibujo.svg", 900 * KB, False, False),
        ("miniatura.jpg", 5 * KB, False, False),
    ],
)
def test_distingue_la_foto_de_un_documento_de_un_logo_o_una_firma(
    nombre: str, tamano: int, incrustada: bool, esperado: bool
) -> None:
    assert ex.es_foto_de_documento(nombre, tamano, incrustada) is esperado


def test_la_foto_de_una_factura_va_con_la_unica_factura_del_correo() -> None:
    plan = _plan(("factura.xml", FACTURA), ("IMG_0001.jpg", b"\xff\xd8\xff"))

    assert f"{CARPETA}/IMG_0001.jpg" in {c.ruta for c in plan.colocaciones}
    assert plan.sin_clasificar == []


def test_una_foto_sola_se_aparta_para_revisarla_a_mano() -> None:
    plan = _plan(("IMG_0001.jpg", b"\xff\xd8\xff"))

    assert plan.colocaciones[0].ruta.startswith("_sin_clasificar/2026-OCTUBRE/")
    assert plan.sin_clasificar == [("IMG_0001.jpg", "imagen")]
    assert plan.comprobantes == []


def test_con_dos_facturas_la_foto_no_se_adivina_y_queda_aparte() -> None:
    otra = FACTURA.replace(b"F010-00000456", b"F010-00000457")
    plan = _plan(("a.xml", FACTURA), ("b.xml", otra), ("foto.png", b"\x89PNG"))

    assert plan.sin_clasificar == [("foto.png", "imagen")]
    assert len(plan.comprobantes) == 2


def test_las_extensiones_en_mayuscula_se_reconocen() -> None:
    plan = _plan(("FACTURA.XML", FACTURA), ("COPIA.PDF", PDF), ("FOTO.JPG", b"\xff\xd8"))

    rutas = {c.ruta for c in plan.colocaciones}
    assert {f"{CARPETA}/FACTURA.XML", f"{CARPETA}/COPIA.PDF", f"{CARPETA}/FOTO.JPG"} <= rutas
    assert plan.sin_clasificar == []


def test_una_foto_dentro_de_un_zip_se_conserva_y_un_logo_no() -> None:
    contenido = _zip({"foto_factura.jpg": b"x" * (200 * KB), "logo.png": b"x" * 500})

    archivos, omitidos = ex.leer_zip(contenido)

    assert [n for n, _ in archivos] == ["foto_factura.jpg"]
    assert omitidos == ["zip_omite_png"]


def test_un_zip_dentro_de_otro_zip_no_se_abre_y_se_informa() -> None:
    archivos, omitidos = ex.leer_zip(_zip({"interno.zip": _zip({"f.xml": FACTURA})}))

    assert archivos == []
    assert omitidos == ["zip_omite_zip"]


# --------------------------------------------------------------------------- #
# Casuísticas de documentos que no cuadran
# --------------------------------------------------------------------------- #
def test_la_serie_numero_del_nombre_no_manda_manda_la_del_xml() -> None:
    plan = _plan(("F999-00000001.xml", FACTURA), ("F999-00000001.pdf", PDF))

    assert {c.ruta for c in plan.colocaciones} == {
        f"{CARPETA}/F999-00000001.xml",
        f"{CARPETA}/F999-00000001.pdf",
    }
    assert (plan.comprobantes[0].serie, plan.comprobantes[0].correlativo) == ("F010", 456)


def test_un_xml_dañado_no_se_pierde_va_a_sin_clasificar() -> None:
    plan = _plan(("roto.xml", b"<Invoice><sin cerrar"))

    assert plan.colocaciones[0].ruta.startswith("_sin_clasificar/")
    assert plan.omitidos == {}
    assert plan.comprobantes == []


def test_un_xml_vacio_o_que_no_es_comprobante_se_aparta() -> None:
    plan = _plan(("vacio.xml", b""), ("otro.xml", b"<html><body/></html>"))

    assert {nombre for nombre, _ in plan.sin_clasificar} == {"vacio.xml", "otro.xml"}


def test_un_xml_ubl_valido_sin_serie_numero_legible_no_rompe_la_extraccion() -> None:
    sin_id = FACTURA.replace(b"F010-00000456", b"")

    plan = _plan(("f.xml", sin_id), ("f.pdf", PDF))

    assert plan.comprobantes == []
    assert {n for n, _ in plan.sin_clasificar} >= {"f.xml", "f.pdf"}


def test_la_guia_con_dos_facturas_no_se_adivina() -> None:
    otra = FACTURA.replace(b"F010-00000456", b"F010-00000457")
    plan = _plan(("a.xml", FACTURA), ("b.xml", otra), ("guia.xml", GUIA))

    assert plan.sin_clasificar == [("guia.xml", "guia")]


def test_dos_facturas_en_un_correo_cada_pdf_se_une_a_la_suya_por_la_serie_numero() -> None:
    otra = FACTURA.replace(b"F010-00000456", b"F010-00000457")
    plan = _plan(
        ("a.xml", FACTURA),
        ("b.xml", otra),
        ("Factura F010-0456.pdf", PDF + b"a"),
        ("Factura F010-457.pdf", PDF + b"b"),
        ("sin-numero.pdf", PDF + b"c"),
    )

    rutas = {c.ruta for c in plan.colocaciones}
    assert f"{CARPETA}/Factura F010-0456.pdf" in rutas  # ceros a la izquierda: igual coincide
    assert "2026/OCTUBRE/20999999999/F010-00000457/Factura F010-457.pdf" in rutas
    # El PDF que no dice de cuál factura es no se aparta: queda en las dos (y llega a R2).
    assert f"{CARPETA}/sin-numero.pdf" in rutas
    assert "2026/OCTUBRE/20999999999/F010-00000457/sin-numero.pdf" in rutas
    assert plan.sin_clasificar == []


def test_los_pdf_adicionales_de_un_correo_con_tres_facturas_no_se_pierden(monkeypatch) -> None:
    """Tres facturas del mismo RUC y varias guías/notas en PDF: cada guía va a su factura."""
    f2 = FACTURA.replace(b"F010-00000456", b"F010-00000457")
    f3 = FACTURA.replace(b"F010-00000456", b"F010-00000458")
    citas = {
        b"%PDF guia-de-la-2": (
            {"20999999999"},
            {("F010", 457)},
        ),
        b"%PDF nota-que-cita-dos": ({"20999999999"}, {("F010", 456), ("F010", 458)}),
    }
    monkeypatch.setattr(ex, "candidatos_de_pdf", lambda datos: citas.get(datos, (set(), set())))

    plan = _plan(
        ("a.xml", FACTURA),
        ("b.xml", f2),
        ("c.xml", f3),
        ("GRR T001-1.pdf", b"%PDF guia-de-la-2"),
        ("nota.pdf", b"%PDF nota-que-cita-dos"),
        ("orden de servicio.pdf", b"%PDF sin-cita"),
    )

    en = lambda nombre: {  # noqa: E731
        c.ruta.rpartition("/")[0].rpartition("/")[2]
        for c in plan.colocaciones
        if c.ruta.endswith("/" + nombre)
    }
    assert en("GRR T001-1.pdf") == {"F010-00000457"}
    assert en("nota.pdf") == {"F010-00000456", "F010-00000458"}
    assert en("orden de servicio.pdf") == {"F010-00000456", "F010-00000457", "F010-00000458"}
    assert plan.sin_clasificar == []
    # La separación por factura no se toca: tres carpetas, cada una con su XML.
    assert sorted(c.carpeta.rpartition("/")[2] for c in plan.comprobantes) == [
        "F010-00000456",
        "F010-00000457",
        "F010-00000458",
    ]
    # Los bytes del adjunto salen idénticos al original del correo.
    copias = [c.datos for c in plan.colocaciones if c.ruta.endswith("/orden de servicio.pdf")]
    assert copias == [b"%PDF sin-cita"] * 3


def test_dos_pdf_en_un_correo_con_una_sola_factura_van_con_ella() -> None:
    plan = _plan(("f.xml", FACTURA), ("factura.pdf", PDF), ("guia.pdf", PDF + b"guia"))

    rutas = {c.ruta for c in plan.colocaciones}
    assert {f"{CARPETA}/factura.pdf", f"{CARPETA}/guia.pdf"} <= rutas
    assert plan.sin_clasificar == []


def test_pdf_sin_ninguna_factura_en_el_correo_sigue_apartado() -> None:
    plan = _plan(("guia.pdf", PDF), ("otra.pdf", PDF + b"x"))

    assert {n for n, _ in plan.sin_clasificar} == {"guia.pdf", "otra.pdf"}


def test_el_mismo_xml_dos_veces_en_un_correo_cae_en_la_misma_carpeta_y_en_disco_queda_uno(
    tmp_path: Path,
) -> None:
    plan = _plan(("a.xml", FACTURA), ("a.xml", FACTURA))

    assert {c.ruta for c in plan.colocaciones} == {f"{CARPETA}/a.xml", f"{CARPETA}/a (2).xml"}
    ex._escribir(tmp_path, plan, ex.Informe(), set())
    assert [p.name for p in (tmp_path / CARPETA).iterdir()] == ["a.xml"]  # idéntico: no se duplica


def test_lo_ya_guardado_en_disco_no_se_duplica_al_repetir_la_extraccion(tmp_path: Path) -> None:
    plan = _plan_con_meta(("f.xml", FACTURA), ("f.pdf", PDF))
    informe = ex.Informe()

    primera = ex._escribir(tmp_path, plan, informe, set())
    segunda = ex._escribir(tmp_path, plan, informe, set())

    assert (primera, segunda) == (3, 0)  # xml + pdf + correo.txt; la 2.ª vez nada es nuevo
    assert sorted(p.name for p in (tmp_path / CARPETA).iterdir()) == [
        "correo.txt",
        "f.pdf",
        "f.xml",
    ]


def test_los_pendientes_se_anotan_sin_duplicar_y_sin_incluir_otras_empresas(
    tmp_path: Path,
) -> None:
    sueltas = "_sin_clasificar/2026-OCTUBRE/x"
    ex.anotar_pendientes(tmp_path, {CARPETA, sueltas, "_otras_empresas/2055/a"})
    ex.anotar_pendientes(tmp_path, {CARPETA})

    anotado = json.loads(
        (tmp_path / "_automatizacion" / "pendientes_importar.json").read_text(encoding="utf-8")
    )
    assert anotado == {"carpetas": [CARPETA], "sin_clasificar": ["_sin_clasificar/2026-OCTUBRE/x"]}


def test_sin_novedades_no_se_crea_el_archivo_de_pendientes(tmp_path: Path) -> None:
    ex.anotar_pendientes(tmp_path, set())

    assert not (tmp_path / "_automatizacion").exists()


def test_mensajes_a_carpetas_copia_el_msg_a_cada_factura(tmp_path: Path) -> None:
    """El `.msg` llega a la carpeta de cada factura (y a la unida por texto); el original queda."""
    correos = tmp_path / "_correos" / "2026-OCTUBRE"
    correos.mkdir(parents=True)
    (correos / "a.msg").write_bytes(b"correo")
    (correos / "a.json").write_text(
        json.dumps(
            {
                "archivos": [
                    {"ruta": "2026/OCTUBRE/20100000001/F001-00000001/f.xml"},
                    {"ruta": "2026/OCTUBRE/20100000001/F001-00000002/f.xml"},
                    {"ruta": "_sin_clasificar/2026-OCTUBRE/a/x.pdf"},
                ]
            }
        ),
        encoding="utf-8",
    )
    for num in ("1", "2"):
        (tmp_path / "2026/OCTUBRE/20100000001" / f"F001-0000000{num}").mkdir(parents=True)
    unida = tmp_path / "2026/OCTUBRE/20100000001/F001-00000003"
    unida.mkdir()
    (unida / "correo.txt").write_text(
        "Correo completo (.msg): _correos/2026-OCTUBRE/a.msg\n", encoding="utf-8"
    )

    ex.mensajes_a_carpetas(ex.argparse.Namespace(destino=str(tmp_path), simular=False))
    ex.mensajes_a_carpetas(
        ex.argparse.Namespace(destino=str(tmp_path), simular=False)
    )  # idempotente

    copias = sorted(p.parent.name for p in tmp_path.glob("2026/*/*/*/correo_a.msg"))
    assert copias == ["F001-00000001", "F001-00000002", "F001-00000003"]
    assert (correos / "a.msg").exists()


class _CarpetaFalsa:
    def __init__(self, nombre: str, tipo: int = 0, hijas: tuple[_CarpetaFalsa, ...] = ()) -> None:
        self.Name = nombre
        self.DefaultItemType = tipo
        self.Folders = list(hijas)


def test_sin_lista_se_leen_todas_las_carpetas_de_correo_menos_las_del_sistema() -> None:
    """Una carpeta nueva de un proveedor (regla de Outlook) se lee sin tener que agregarla."""
    raiz = _CarpetaFalsa(
        "buzon",
        hijas=(
            _CarpetaFalsa("Bandeja de entrada", hijas=(_CarpetaFalsa("TRANSMIDAL"),)),
            _CarpetaFalsa("GRUPO AR&J"),
            _CarpetaFalsa("Elementos enviados"),
            _CarpetaFalsa("Problemas de sincronización"),
            _CarpetaFalsa("Calendario", tipo=1),
        ),
    )

    leidas = [c.Name for c in ex._carpetas(raiz, {ex.TODAS_LAS_CARPETAS})]
    assert leidas == ["Bandeja de entrada", "TRANSMIDAL", "GRUPO AR&J"]
    assert [c.Name for c in ex._carpetas(raiz, {"grupo ar&j"})] == ["GRUPO AR&J"]
