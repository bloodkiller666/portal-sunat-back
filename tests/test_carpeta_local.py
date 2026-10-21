"""Carpeta local de respaldo: apertura inmediata (sin base de datos) de una carpeta existente."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings

PARTES = ["2026", "09", "FF01-00000123", "20100000001"]
URL = "/api/v1/carpeta-local/abrir"


@pytest.fixture
def abiertas(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    rutas: list[Path] = []
    monkeypatch.setattr("app.services.carpeta_local._abrir_en_explorador", rutas.append)
    return rutas


def test_deshabilitado_por_defecto_devuelve_409(
    client: TestClient, auth: dict[str, str], abiertas: list[Path]
) -> None:
    r = client.post(URL, headers=auth, json={"partes": PARTES})
    assert r.status_code == 409
    assert r.json()["error"]["details"] == {"motivo": "deshabilitado"}
    assert abiertas == []


def test_exige_autenticacion(client: TestClient, settings: Settings) -> None:
    settings.local_abrir_carpeta = True
    assert client.post(URL, json={"partes": PARTES}).status_code == 401


def test_abre_la_carpeta_si_existe(
    client: TestClient,
    auth: dict[str, str],
    settings: Settings,
    tmp_path: Path,
    abiertas: list[Path],
) -> None:
    settings.local_abrir_carpeta = True
    settings.local_descargas_dir = tmp_path
    destino = tmp_path.joinpath(*PARTES)
    destino.mkdir(parents=True)

    r = client.post(URL, headers=auth, json={"partes": PARTES})

    assert r.status_code == 200
    assert Path(r.json()["ruta"]) == destino.resolve()
    assert abiertas == [destino.resolve()]


def test_si_la_carpeta_no_existe_devuelve_404_y_no_abre_nada(
    client: TestClient,
    auth: dict[str, str],
    settings: Settings,
    tmp_path: Path,
    abiertas: list[Path],
) -> None:
    settings.local_abrir_carpeta = True
    settings.local_descargas_dir = tmp_path

    r = client.post(URL, headers=auth, json={"partes": PARTES})

    assert r.status_code == 404
    assert abiertas == []


def test_no_puede_salir_de_la_carpeta_base(
    client: TestClient,
    auth: dict[str, str],
    settings: Settings,
    tmp_path: Path,
    abiertas: list[Path],
) -> None:
    base = tmp_path / "base"
    (tmp_path / "secreto").mkdir(parents=True)
    base.mkdir()
    settings.local_abrir_carpeta = True
    settings.local_descargas_dir = base

    r = client.post(URL, headers=auth, json={"partes": ["..", "..", "secreto", ".."]})

    assert r.status_code == 404
    assert abiertas == []


def test_exige_exactamente_cuatro_partes(
    client: TestClient, auth: dict[str, str], settings: Settings
) -> None:
    settings.local_abrir_carpeta = True
    assert client.post(URL, headers=auth, json={"partes": ["2026"]}).status_code == 422
