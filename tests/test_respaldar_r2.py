"""Pruebas de la lógica del respaldo de R2 (sin red)."""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path

import pytest
from scripts.respaldar_r2 import (
    NOMBRE_MANIFIESTO,
    ObjetoRemoto,
    escribir_manifiesto,
    planificar,
    ruta_local,
)


def test_solo_se_baja_lo_que_falta_o_cambio_de_tamano(tmp_path: Path) -> None:
    (tmp_path / "sire" / "202609").mkdir(parents=True)
    (tmp_path / "sire" / "202609" / "a.zip").write_bytes(b"12345")
    (tmp_path / "comprobantes").mkdir()
    (tmp_path / "comprobantes" / "b.xml").write_bytes(b"viejo")
    remotos = [
        ObjetoRemoto("sire/202609/a.zip", 5),
        ObjetoRemoto("comprobantes/b.xml", 99),
        ObjetoRemoto("comprobantes/c.xml", 10),
    ]

    pendientes, al_dia = planificar(remotos, tmp_path)

    assert al_dia == 1
    assert [p.clave for p in pendientes] == ["comprobantes/b.xml", "comprobantes/c.xml"]


@pytest.mark.parametrize("clave", ["../fuera.txt", "a/../../fuera.txt", "/etc/passwd"])
def test_una_clave_que_se_sale_de_la_carpeta_se_rechaza(tmp_path: Path, clave: str) -> None:
    with pytest.raises(ValueError):
        ruta_local(tmp_path, clave)


def test_el_manifiesto_trae_el_hash_real_de_cada_archivo(tmp_path: Path) -> None:
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "uno.xml").write_bytes(b"contenido")
    remotos = [ObjetoRemoto("x/uno.xml", 9), ObjetoRemoto("x/falta.xml", 3)]

    filas = escribir_manifiesto(tmp_path, remotos)

    assert filas == 1  # lo que no se pudo bajar no figura como respaldado
    with (tmp_path / NOMBRE_MANIFIESTO).open(encoding="utf-8") as archivo:
        registros = list(csv.DictReader(archivo))
    assert registros == [
        {"clave": "x/uno.xml", "bytes": "9", "sha256": hashlib.sha256(b"contenido").hexdigest()}
    ]
