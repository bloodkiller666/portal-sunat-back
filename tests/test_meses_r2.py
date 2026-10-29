"""La migración de R2 a meses en letras solo toca las claves de comprobantes con mes numérico."""

from scripts.meses_en_letras_r2 import clave_en_letras


def test_pasa_el_mes_a_letras() -> None:
    clave = "comprobantes/20514831620/2026/10/FF01-00016077/20514831620-01-FF01-00016077.xml"
    assert clave_en_letras(clave) == clave.replace("/10/", "/OCTUBRE/")
    assert clave_en_letras("comprobantes/20514831620/2026/09/F/x.xml") == (
        "comprobantes/20514831620/2026/SETIEMBRE/F/x.xml"
    )


def test_no_toca_lo_que_ya_esta_en_letras_ni_otras_rutas() -> None:
    assert clave_en_letras("comprobantes/20514831620/2026/OCTUBRE/F/x.xml") is None
    assert clave_en_letras("sire/20514831620/202610/zip.zip") is None
    assert clave_en_letras("comprobantes/20514831620/2026/13/F/x.xml") is None
