"""La migración de carpetas locales al mes «10. OCTUBRE» renombra y corrige las rutas citadas."""

from pathlib import Path

from scripts.meses_en_carpetas_locales import (
    carpeta_nueva,
    corregir_texto,
    migrar_compras,
    periodo_nuevo,
)


def test_cada_forma_antigua_pasa_al_formato_actual() -> None:
    assert carpeta_nueva("OCTUBRE") == "10. OCTUBRE"
    assert carpeta_nueva("09") == "9. SETIEMBRE"
    assert carpeta_nueva("10. OCTUBRE") is None  # ya está bien
    assert carpeta_nueva("sin-fecha") is None
    assert periodo_nuevo("2026-AGOSTO") == "2026-8. AGOSTO"
    assert periodo_nuevo("2026-10. OCTUBRE") is None


def test_las_rutas_citadas_en_los_textos_siguen_a_las_carpetas() -> None:
    texto = (
        '{"ruta": "2026/OCTUBRE/20999999999/F010-00000456/f.xml", '
        '"mensaje": "_correos/2026-OCTUBRE/20261021-160605_abcdef.msg", '
        '"otra": "_otras_empresas/2055/2026/JULIO/20999999999/F010-1", "recibido": "2026-10-21"}'
    )

    corregido = corregir_texto(texto)

    assert "2026/10. OCTUBRE/20999999999/F010-00000456/f.xml" in corregido
    assert "_correos/2026-10. OCTUBRE/20261021-160605_abcdef.msg" in corregido
    assert "2026/7. JULIO/20999999999/F010-1" in corregido
    assert '"recibido": "2026-10-21"' in corregido  # una fecha no es una carpeta
    assert corregir_texto(corregido) == corregido  # idempotente


def _armar(base: Path) -> None:
    (base / "2026" / "OCTUBRE" / "20999999999" / "F010-1").mkdir(parents=True)
    (base / "2026" / "OCTUBRE" / "20999999999" / "F010-1" / "f.xml").write_bytes(b"x")
    (base / "2026" / "10" / "20888888888" / "F010-2").mkdir(parents=True)
    (base / "_otras_empresas" / "2055" / "2026" / "AGOSTO" / "2077").mkdir(parents=True)
    (base / "_correos" / "2026-OCTUBRE").mkdir(parents=True)
    (base / "_correos" / "2026-OCTUBRE" / "a.msg").write_bytes(b"msg")
    (base / "_correos" / "2026-OCTUBRE" / "a.json").write_text(
        '{"ruta": "2026/OCTUBRE/20999999999/F010-1/f.xml"}', encoding="utf-8"
    )
    (base / "_sin_clasificar" / "2026-SETIEMBRE" / "x").mkdir(parents=True)


def test_renombra_las_carpetas_y_corrige_los_textos(tmp_path: Path) -> None:
    _armar(tmp_path)

    resumen = migrar_compras(tmp_path, aplicar=True, conflictos=[])

    assert (
        tmp_path / "2026" / "10. OCTUBRE" / "20999999999" / "F010-1" / "f.xml"
    ).read_bytes() == b"x"
    assert (
        tmp_path / "2026" / "10. OCTUBRE" / "20888888888" / "F010-2"
    ).is_dir()  # el `10` se funde
    assert not (tmp_path / "2026" / "OCTUBRE").exists() and not (tmp_path / "2026" / "10").exists()
    assert (tmp_path / "_otras_empresas" / "2055" / "2026" / "8. AGOSTO" / "2077").is_dir()
    assert (tmp_path / "_correos" / "2026-10. OCTUBRE" / "a.msg").read_bytes() == b"msg"
    assert (tmp_path / "_sin_clasificar" / "2026-9. SETIEMBRE" / "x").is_dir()
    texto = (tmp_path / "_correos" / "2026-10. OCTUBRE" / "a.json").read_text(encoding="utf-8")
    assert "2026/10. OCTUBRE/20999999999" in texto
    assert (resumen["carpetas_de_mes"], resumen["otras_empresas"], resumen["auditoria"]) == (
        2,
        1,
        2,
    )
    assert migrar_compras(tmp_path, aplicar=True, conflictos=[]) == {
        "carpetas_de_mes": 0,
        "otras_empresas": 0,
        "auditoria": 0,
        "textos": 0,
    }


def test_simular_no_cambia_nada(tmp_path: Path) -> None:
    _armar(tmp_path)

    resumen = migrar_compras(tmp_path, aplicar=False, conflictos=[])

    assert resumen["carpetas_de_mes"] == 2
    assert (tmp_path / "2026" / "OCTUBRE").is_dir()
    assert (tmp_path / "_correos" / "2026-OCTUBRE").is_dir()


def test_al_fundir_no_pisa_un_archivo_que_ya_existe(tmp_path: Path) -> None:
    (tmp_path / "2026" / "OCTUBRE" / "R" / "D").mkdir(parents=True)
    (tmp_path / "2026" / "OCTUBRE" / "R" / "D" / "f.pdf").write_bytes(b"viejo")
    (tmp_path / "2026" / "10. OCTUBRE" / "R" / "D").mkdir(parents=True)
    (tmp_path / "2026" / "10. OCTUBRE" / "R" / "D" / "f.pdf").write_bytes(b"nuevo")
    conflictos: list[str] = []

    migrar_compras(tmp_path, aplicar=True, conflictos=conflictos)

    assert (tmp_path / "2026" / "10. OCTUBRE" / "R" / "D" / "f.pdf").read_bytes() == b"nuevo"
    assert len(conflictos) == 1 and conflictos[0].endswith("f.pdf")
    assert (tmp_path / "2026" / "OCTUBRE" / "R" / "D" / "f.pdf").read_bytes() == b"viejo"
