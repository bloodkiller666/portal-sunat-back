"""Publicar en la red solo las carpetas de año, sin llevar los apoyos del extractor."""

from __future__ import annotations

import os
import time
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

    assert r == {"copiados": 1, "ya_estaban": 0, "sin_revisar": 0, "errores": []}
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


def test_incremental_solo_mira_lo_reciente_y_la_completa_recupera_lo_demas(
    tmp_path: Path,
) -> None:
    local, red = tmp_path / "local", tmp_path / "red"
    vieja = _crear(local, "2026/10. OCTUBRE/20100000001/F001-1/F001-1.xml")
    hace_un_mes = time.time() - 30 * 86400
    os.utime(vieja, (hace_un_mes, hace_un_mes))
    time.sleep(0.05)
    corte = time.time()  # entre la creación de la vieja y la de la nueva
    time.sleep(0.05)
    _crear(local, "2026/11. NOVIEMBRE/20100000001/F001-2/F001-2.xml")

    # La vieja nunca se publicó, pero un incremental no la mira (eso lo cubre la pasada completa).
    r = publicar_anios(local, red, simular=False, modificados_desde=corte, listar=True)
    assert r["copiados"] == 1 and r["sin_revisar"] == 1
    assert r["archivos"] == ["2026/11. NOVIEMBRE/20100000001/F001-2/F001-2.xml"]
    assert not (red / "2026/10. OCTUBRE/20100000001/F001-1/F001-1.xml").exists()

    completa = publicar_anios(local, red, simular=False)
    assert completa["copiados"] == 1 and completa["ya_estaban"] == 1
    assert (red / "2026/10. OCTUBRE/20100000001/F001-1/F001-1.xml").exists()


def test_incremental_y_completa_copian_lo_mismo_si_nada_se_quedo_atras(tmp_path: Path) -> None:
    local, red = tmp_path / "local", tmp_path / "red"
    viejo = _crear(local, "2026/10. OCTUBRE/20100000001/F001-1/F001-1.xml")
    _crear(red, "2026/10. OCTUBRE/20100000001/F001-1/F001-1.xml")  # ya publicado
    _crear(local, "2026/11. NOVIEMBRE/20100000001/F001-2/F001-2.xml")
    ayer = time.time() - 86400
    os.utime(viejo, (ayer, ayer))
    os.utime(red / "2026/10. OCTUBRE/20100000001/F001-1/F001-1.xml", (ayer, ayer))
    corte = time.time() - 3600
    os.utime(local / "2026/11. NOVIEMBRE/20100000001/F001-2/F001-2.xml", (corte + 60, corte + 60))

    incremental = publicar_anios(local, red, simular=True, modificados_desde=corte, listar=True)
    completa = publicar_anios(local, red, simular=True, listar=True)
    assert incremental["archivos"] == completa["archivos"]
