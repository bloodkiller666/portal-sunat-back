"""El índice de PDF sueltos clasifica cada uno por motivo y enlaza carpeta y archivo."""

import hashlib
from pathlib import Path

import scripts.indice_sueltos as ind

TEXTO = "Factura electrónica del proveedor con RUC 20123456789 y detalle suficiente de texto"


def test_el_motivo_depende_de_la_huella_el_texto_y_lo_que_cita(monkeypatch) -> None:
    textos = {b"a": "", b"b": TEXTO, b"c": TEXTO + " guía T001-123"}
    monkeypatch.setattr(ind, "texto_del_pdf", lambda datos: textos.get(datos, ""))
    series = {b"b": (set(), set()), b"c": ({"20123456789"}, {("T001", 123)})}
    monkeypatch.setattr(ind, "candidatos_de_pdf", lambda datos: series[datos])
    ya = {hashlib.sha256(b"z").hexdigest()}

    assert ind.clasificar(b"z", ya) == "ya_en_portal"
    assert ind.clasificar(b"a", ya) == "escaneo"
    assert ind.clasificar(b"b", ya) == "sin_serie"
    assert ind.clasificar(b"c", ya) == "cita_otro"


def test_el_html_enlaza_la_carpeta_del_correo_y_cada_pdf(tmp_path: Path) -> None:
    correo = tmp_path / "2026-OCTUBRE" / "20261021-160605_abcdef"
    correo.mkdir(parents=True)
    (correo / "correo.txt").write_text(
        "Asunto:    Orden <4500054571>\nDe:        Proveedor SAC\nRecibido:  2026-10-21 16:06\n",
        encoding="utf-8",
    )
    pdf = correo / "4500054571.pdf"
    pdf.write_bytes(b"%PDF")

    pagina = ind.construir_html([ind.Suelto(pdf, "sin_serie", correo)], tmp_path)

    assert correo.as_uri() in pagina  # enlace a la carpeta
    assert pdf.as_uri() in pagina  # y al PDF
    assert "Orden &lt;4500054571&gt;" in pagina  # el asunto va escapado
    assert "Proveedor SAC" in pagina
    assert ind.MOTIVOS["sin_serie"] in pagina
