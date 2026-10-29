"""Los meses de las carpetas van en letras; las anteriores (`2026/10`) se siguen abriendo."""

from datetime import date

from app.core.meses import NOMBRES_MES, mes_numerico, nombre_mes, periodo_carpeta
from app.services.carpeta_local import _variantes_de_mes


def test_cada_mes_tiene_su_nombre() -> None:
    assert len(NOMBRES_MES) == 12
    assert [nombre_mes(m) for m in (1, 9, 10, 11, 12)] == [
        "ENERO",
        "SETIEMBRE",
        "OCTUBRE",
        "NOVIEMBRE",
        "DICIEMBRE",
    ]
    assert periodo_carpeta(date(2026, 10, 29)) == "2026-OCTUBRE"


def test_del_nombre_al_numero_de_antes() -> None:
    assert mes_numerico("octubre") == "10"
    assert mes_numerico("SETIEMBRE") == "09"
    assert mes_numerico("sin-fecha") is None
    assert mes_numerico("10") is None


def test_abrir_prueba_primero_el_nombre_y_despues_el_numero() -> None:
    assert _variantes_de_mes(["2026", "OCTUBRE", "20123456789", "F001-00000001"]) == [
        ["2026", "OCTUBRE", "20123456789", "F001-00000001"],
        ["2026", "10", "20123456789", "F001-00000001"],
    ]
    assert _variantes_de_mes(["2026", "sin-fecha", "x", "y"]) == [["2026", "sin-fecha", "x", "y"]]
