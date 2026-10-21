"""Carpeta local de respaldo: ruta derivada del comprobante y apertura en el Explorador."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.enums import OrigenComprobante
from app.db.models import Company
from app.services.carpeta_local import partes_de_carpeta
from tests.conftest import RUC_PRINCIPAL
from tests.fakes import FakeUnitOfWork, construir_comprobante


@pytest.fixture
def abiertas(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    rutas: list[Path] = []
    monkeypatch.setattr("app.services.carpeta_local._abrir_en_explorador", rutas.append)
    return rutas


def _comprobante(uow: FakeUnitOfWork, empresa: Company, **campos):
    c = construir_comprobante(
        empresa.id,
        ruc=RUC_PRINCIPAL,
        serie="FF01",
        correlativo=123,
        fecha_emision=date(2026, 9, 5),
        **campos,
    )
    c.ruc_receptor = "20100000001"
    uow.comprobantes.comprobantes[c.id] = c
    return c


def test_la_estructura_es_anio_mes_documento_ruc(
    uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    c = _comprobante(uow, empresa_principal)
    assert partes_de_carpeta(c) == ["2026", "09", "FF01-00000123", "20100000001"]


def test_una_compra_usa_el_ruc_del_proveedor(
    uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    c = _comprobante(uow, empresa_principal, origen=OrigenComprobante.recibido)
    assert partes_de_carpeta(c)[3] == RUC_PRINCIPAL


def test_deshabilitado_por_defecto_devuelve_409(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    c = _comprobante(uow, empresa_principal)
    r = client.post(f"/api/v1/comprobantes/{c.id}/abrir-carpeta", headers=auth)
    assert r.status_code == 409
    assert r.json()["error"]["details"] == {"motivo": "deshabilitado"}


def test_abre_la_carpeta_si_existe(
    client: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    empresa_principal: Company,
    settings: Settings,
    tmp_path: Path,
    abiertas: list[Path],
) -> None:
    settings.local_abrir_carpeta = True
    settings.local_descargas_dir = tmp_path
    c = _comprobante(uow, empresa_principal)
    destino = tmp_path / "2026" / "09" / "FF01-00000123" / "20100000001"
    destino.mkdir(parents=True)

    r = client.post(f"/api/v1/comprobantes/{c.id}/abrir-carpeta", headers=auth)

    assert r.status_code == 200
    assert Path(r.json()["ruta"]) == destino.resolve()
    assert abiertas == [destino.resolve()]


def test_si_la_carpeta_no_existe_devuelve_404_y_no_abre_nada(
    client: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    empresa_principal: Company,
    settings: Settings,
    tmp_path: Path,
    abiertas: list[Path],
) -> None:
    settings.local_abrir_carpeta = True
    settings.local_descargas_dir = tmp_path
    c = _comprobante(uow, empresa_principal)

    r = client.post(f"/api/v1/comprobantes/{c.id}/abrir-carpeta", headers=auth)

    assert r.status_code == 404
    assert abiertas == []


def test_comprobante_inexistente_devuelve_404(
    client: TestClient, auth: dict[str, str], settings: Settings
) -> None:
    settings.local_abrir_carpeta = True
    r = client.post(f"/api/v1/comprobantes/{uuid4()}/abrir-carpeta", headers=auth)
    assert r.status_code == 404
