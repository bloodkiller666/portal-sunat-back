"""Los meses de las carpetas van como «10. OCTUBRE»; las anteriores se siguen abriendo."""

from datetime import date

from app.core.meses import (
    NOMBRES_MES,
    carpeta_de_mes,
    es_carpeta_de_mes,
    mes_numerico,
    nombre_mes,
    numero_de_mes,
    periodo_carpeta,
    variantes_de_mes,
)
from app.services.carpeta_local import _variantes_de_mes
from app.storage.base import construir_clave


def test_cada_mes_tiene_su_nombre_y_su_carpeta() -> None:
    assert len(NOMBRES_MES) == 12
    assert [nombre_mes(m) for m in (1, 9, 10, 12)] == ["ENERO", "SETIEMBRE", "OCTUBRE", "DICIEMBRE"]
    assert [carpeta_de_mes(m) for m in (1, 2, 9, 10, 12)] == [
        "1. ENERO",
        "2. FEBRERO",
        "9. SETIEMBRE",
        "10. OCTUBRE",
        "12. DICIEMBRE",
    ]
    assert periodo_carpeta(date(2026, 10, 29)) == "2026-10. OCTUBRE"


def test_el_mes_se_reconoce_en_cualquiera_de_sus_formas() -> None:
    assert numero_de_mes("10. OCTUBRE") == 10
    assert numero_de_mes("octubre") == 10
    assert numero_de_mes("09") == 9
    assert numero_de_mes("9") == 9
    assert numero_de_mes("10. AGOSTO") is None  # número y nombre no coinciden
    assert numero_de_mes("sin-fecha") is None
    assert numero_de_mes("13") is None
    assert es_carpeta_de_mes("10. OCTUBRE") and not es_carpeta_de_mes("OCTUBRE")
    assert mes_numerico("SETIEMBRE") == "09"


def test_las_variantes_van_de_la_actual_a_la_mas_antigua() -> None:
    assert variantes_de_mes("OCTUBRE") == ["10. OCTUBRE", "OCTUBRE", "10"]
    assert variantes_de_mes("1. ENERO") == ["1. ENERO", "ENERO", "01"]
    assert variantes_de_mes("sin-fecha") == ["sin-fecha"]


def test_abrir_prueba_el_formato_actual_y_despues_los_de_antes() -> None:
    resto = ["20123456789", "F001-00000001"]
    assert _variantes_de_mes(["2026", "OCTUBRE", *resto]) == [
        ["2026", "10. OCTUBRE", *resto],
        ["2026", "OCTUBRE", *resto],
        ["2026", "10", *resto],
    ]
    assert _variantes_de_mes(["2026", "sin-fecha", "x", "y"]) == [["2026", "sin-fecha", "x", "y"]]


def test_la_clave_de_r2_conserva_el_espacio_del_mes() -> None:
    clave = construir_clave("comprobantes/20514831620/2026/10. OCTUBRE/FF01-1", "a b (2).pdf")
    assert clave == "comprobantes/20514831620/2026/10. OCTUBRE/FF01-1/a_b_2_.pdf"
