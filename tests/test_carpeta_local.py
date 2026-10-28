"""Carpeta local de respaldo: apertura inmediata (sin base de datos) de una carpeta existente."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.enums import EstadoArchivo, OrigenComprobante, TipoArchivo
from app.db.models import Archivo, Company, Comprobante
from tests.fakes import FakeStorageProvider, FakeUnitOfWork, construir_comprobante

PARTES = ["2026", "09", "20100000001", "FF01-00000123"]
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


def test_abre_la_carpeta_de_proveedores_si_no_esta_en_la_base(
    client: TestClient,
    auth: dict[str, str],
    settings: Settings,
    tmp_path: Path,
    abiertas: list[Path],
) -> None:
    settings.local_abrir_carpeta = True
    settings.local_descargas_dir = tmp_path / "SIRE"
    settings.local_proveedores_dir = tmp_path / "FacturasProveedores"
    destino = settings.local_proveedores_dir.joinpath(*PARTES)
    destino.mkdir(parents=True)

    r = client.post(URL, headers=auth, json={"partes": PARTES})

    assert r.status_code == 200
    assert abiertas == [destino.resolve()]


def test_cada_apartado_mira_solo_su_carpeta(
    client: TestClient,
    auth: dict[str, str],
    settings: Settings,
    tmp_path: Path,
    abiertas: list[Path],
) -> None:
    settings.local_abrir_carpeta = True
    settings.local_descargas_dir = tmp_path / "SIRE"
    settings.local_proveedores_dir = tmp_path / "FacturasProveedores"
    ventas = settings.local_descargas_dir.joinpath(*PARTES)
    compras = settings.local_proveedores_dir.joinpath(*PARTES)
    ventas.mkdir(parents=True)
    compras.mkdir(parents=True)

    emitido = client.post(URL, headers=auth, json={"partes": PARTES, "origen": "emitido"})
    recibido = client.post(URL, headers=auth, json={"partes": PARTES, "origen": "recibido"})

    assert Path(emitido.json()["ruta"]) == ventas.resolve()
    assert Path(recibido.json()["ruta"]) == compras.resolve()
    assert abiertas == [ventas.resolve(), compras.resolve()]


def test_una_venta_no_abre_la_carpeta_de_compras(
    client: TestClient,
    auth: dict[str, str],
    settings: Settings,
    tmp_path: Path,
    abiertas: list[Path],
) -> None:
    settings.local_abrir_carpeta = True
    settings.local_descargas_dir = tmp_path / "SIRE"
    settings.local_proveedores_dir = tmp_path / "FacturasProveedores"
    settings.local_proveedores_dir.joinpath(*PARTES).mkdir(parents=True)

    r = client.post(URL, headers=auth, json={"partes": PARTES, "origen": "emitido"})

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


# --------------------------------------------------------------------------- #
# Restaurar desde el bucket: comprobantes con archivos en R2 pero sin carpeta en la PC
# --------------------------------------------------------------------------- #
RESTAURAR = "/api/v1/carpeta-local/restaurar-y-abrir"


def _compra_con_archivos(
    uow: FakeUnitOfWork,
    empresa: Company,
    almacenamiento: FakeStorageProvider,
    tipos: tuple[TipoArchivo, ...] = (TipoArchivo.pdf,),
) -> Comprobante:
    compra = construir_comprobante(
        empresa.id,
        ruc="20612414328",
        serie="E001",
        correlativo=278,
        fecha_emision=date(2026, 10, 23),
        origen=OrigenComprobante.recibido,
    )
    uow.comprobantes.comprobantes[compra.id] = compra
    for tipo in tipos:
        clave = f"comprobantes/20612414328/2026/10/E001-00000278/doc.{tipo.value}"
        almacenamiento.objetos[clave] = f"contenido {tipo.value}".encode()
        uow.archivos.archivos.append(
            Archivo(
                id=uuid4(),
                company_id=empresa.id,
                comprobante_id=compra.id,
                tipo=tipo,
                nombre=f"doc.{tipo.value}",
                mime_type="application/octet-stream",
                size_bytes=10,
                hash_sha256="a" * 64,
                storage_provider="r2",
                storage_bucket="bucket-privado",
                storage_key=clave,
                estado=EstadoArchivo.subido,
                subido_at=datetime.now(tz=UTC),
            )
        )
    return compra


@pytest.fixture
def restauracion(
    settings: Settings,
    almacenamiento: FakeStorageProvider,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    settings.local_abrir_carpeta = True
    settings.local_descargas_dir = tmp_path / "SIRE"
    settings.local_proveedores_dir = tmp_path / "FacturasProveedores"
    monkeypatch.setattr(
        "app.services.carpeta_local.get_storage_provider", lambda _s: almacenamiento
    )
    return tmp_path


def test_restaura_la_carpeta_de_una_compra_desde_el_bucket(
    client: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    empresa_principal: Company,
    almacenamiento: FakeStorageProvider,
    restauracion: Path,
    abiertas: list[Path],
) -> None:
    compra = _compra_con_archivos(uow, empresa_principal, almacenamiento)

    r = client.post(RESTAURAR, headers=auth, json={"comprobante_id": str(compra.id)})

    destino = restauracion / "FacturasProveedores" / "2026" / "10" / "20612414328" / "E001-00000278"
    assert r.status_code == 200
    assert Path(r.json()["ruta"]) == destino.resolve()
    assert (destino / "doc.pdf").read_bytes() == b"contenido pdf"
    assert not list(destino.glob("*.parcial"))
    assert abiertas == [destino.resolve()]


def test_restaurar_no_pisa_lo_que_ya_hay_ni_copia_el_cdr_de_una_compra(
    client: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    empresa_principal: Company,
    almacenamiento: FakeStorageProvider,
    restauracion: Path,
    abiertas: list[Path],
) -> None:
    compra = _compra_con_archivos(
        uow, empresa_principal, almacenamiento, (TipoArchivo.pdf, TipoArchivo.xml, TipoArchivo.cdr)
    )
    destino = restauracion / "FacturasProveedores" / "2026" / "10" / "20612414328" / "E001-00000278"
    destino.mkdir(parents=True)
    (destino / "doc.pdf").write_bytes(b"version local")

    r = client.post(RESTAURAR, headers=auth, json={"comprobante_id": str(compra.id)})

    assert r.status_code == 200
    assert (destino / "doc.pdf").read_bytes() == b"version local"
    assert (destino / "doc.xml").read_bytes() == b"contenido xml"
    assert not (destino / "doc.cdr").exists()


def test_restaurar_sin_archivos_devuelve_404(
    client: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    empresa_principal: Company,
    almacenamiento: FakeStorageProvider,
    restauracion: Path,
    abiertas: list[Path],
) -> None:
    compra = _compra_con_archivos(uow, empresa_principal, almacenamiento, ())

    r = client.post(RESTAURAR, headers=auth, json={"comprobante_id": str(compra.id)})

    assert r.status_code == 404
    assert r.json()["error"]["details"] == {"motivo": "sin_archivos"}
    assert abiertas == []


def test_restaurar_comprobante_ajeno_o_inexistente_devuelve_404(
    client: TestClient, auth: dict[str, str], restauracion: Path, abiertas: list[Path]
) -> None:
    r = client.post(RESTAURAR, headers=auth, json={"comprobante_id": str(uuid4())})

    assert r.status_code == 404
    assert abiertas == []


def test_restaurar_deshabilitado_devuelve_409(
    client: TestClient, auth: dict[str, str], abiertas: list[Path]
) -> None:
    r = client.post(RESTAURAR, headers=auth, json={"comprobante_id": str(uuid4())})

    assert r.status_code == 409
    assert r.json()["error"]["details"] == {"motivo": "deshabilitado"}


def test_restaurar_exige_autenticacion(client: TestClient) -> None:
    assert client.post(RESTAURAR, json={"comprobante_id": str(uuid4())}).status_code == 401
