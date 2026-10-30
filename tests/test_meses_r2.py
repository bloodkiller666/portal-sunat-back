"""La migración de R2 al mes «10. OCTUBRE» solo toca las claves de comprobantes con otro formato."""

from scripts.meses_en_letras_r2 import clave_con_mes_actual


def test_pasa_el_mes_numerico_o_en_letras_al_formato_actual() -> None:
    clave = "comprobantes/20514831620/2026/10/FF01-00016077/20514831620-01-FF01-00016077.xml"
    assert clave_con_mes_actual(clave) == clave.replace("/10/", "/10. OCTUBRE/")
    assert clave_con_mes_actual("comprobantes/20514831620/2026/09/F/x.xml") == (
        "comprobantes/20514831620/2026/9. SETIEMBRE/F/x.xml"
    )
    assert clave_con_mes_actual("comprobantes/20514831620/2026/AGOSTO/F/x.xml") == (
        "comprobantes/20514831620/2026/8. AGOSTO/F/x.xml"
    )


def test_no_toca_lo_que_ya_esta_bien_ni_otras_rutas() -> None:
    assert clave_con_mes_actual("comprobantes/20514831620/2026/10. OCTUBRE/F/x.xml") is None
    assert clave_con_mes_actual("sire/20514831620/202610/zip.zip") is None
    assert clave_con_mes_actual("comprobantes/20514831620/2026/13/F/x.xml") is None
    assert clave_con_mes_actual("comprobantes/20514831620/2026/10. AGOSTO/F/x.xml") is None
