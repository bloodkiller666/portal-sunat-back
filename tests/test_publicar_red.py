"""Publicar en la red solo las carpetas de año, sin llevar los apoyos del extractor."""

from __future__ import annotations

import os
from pathlib import Path

from scripts.publicar_red import publicar_anios


def _crear(base: Path, ruta: str, texto: str = "x") -> Path:
    p = base / ruta
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(texto, encoding="utf-8")
    return p


def test_copia_solo_las_carpetas_de_anio(tmp_path: Path) -> None:
    local, red = tmp_path / "local", tmp_path / "red"
    _crear(local, "2026/11. NOVIEMBRE/20100000001/F001-1/F001-1.xml")
    _crear(local, "_correos/2026-11. NOVIEMBRE/a.json")
    _crear(local, "_sin_clasificar/x/y.pdf")
    _crear(local, "registro_correos.jsonl")

    r = publicar_anios(local, red, simular=False)

    assert r == {"copiados": 1, "ya_estaban": 0, "errores": []}
    assert (red / "2026/11. NOVIEMBRE/20100000001/F001-1/F001-1.xml").exists()
    assert sorted(p.name for p in red.iterdir()) == ["2026"]


def test_simular_no_escribe(tmp_path: Path) -> None:
    local, red = tmp_path / "local", tmp_path / "red"
    _crear(local, "2026/10. OCTUBRE/20100000001/F001-1/a.xml")

    r = publicar_anios(local, red, simular=True)

    assert r["copiados"] == 1
    assert not red.exists()


def test_no_repite_lo_ya_publicado_y_actualiza_lo_cambiado(tmp_path: Path) -> None:
    local, red = tmp_path / "local", tmp_path / "red"
    origen = _crear(local, "2026/10. OCTUBRE/20100000001/F001-1/a.xml", "uno")
    publicar_anios(local, red, simular=False)

    assert publicar_anios(local, red, simular=False)["ya_estaban"] == 1

    origen.write_text("dos más largo", encoding="utf-8")
    os.utime(origen, (origen.stat().st_atime, origen.stat().st_mtime + 10))
    r = publicar_anios(local, red, simular=False)
    assert r["copiados"] == 1
    assert (red / "2026/10. OCTUBRE/20100000001/F001-1/a.xml").read_text(encoding="utf-8") == (
        "dos más largo"
    )


def test_nunca_borra_lo_que_solo_esta_en_la_red(tmp_path: Path) -> None:
    local, red = tmp_path / "local", tmp_path / "red"
    _crear(local, "2026/10. OCTUBRE/20100000001/F001-1/a.xml")
    solo_red = _crear(red, "2026/10. OCTUBRE/20100000002/F002-2/b.xml")

    publicar_anios(local, red, simular=False)

    assert solo_red.exists()


def test_origen_inexistente_no_falla(tmp_path: Path) -> None:
    assert publicar_anios(tmp_path / "no_existe", tmp_path / "red", simular=False)["copiados"] == 0
