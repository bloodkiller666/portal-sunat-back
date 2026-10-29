"""Carpeta local de respaldo: apertura inmediata (sin base de datos) de una carpeta existente."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.enums import EstadoArchivo, OrigenComprobante, TipoArchivo
from app.db.models import Archivo, Company, Comprobante
from app.services.correo_compras import texto_correo_de_compra
from app.services.evidencia_correos import completar_evidencia_de_correos
from tests.fakes import FakeStorageProvider, FakeUnitOfWork, construir_comprobante

PARTES = ["2026", "SETIEMBRE", "20100000001", "FF01-00000123"]
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

    destino = (
        restauracion / "FacturasProveedores" / "2026" / "OCTUBRE" / "20612414328" / "E001-00000278"
    )
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
    destino = (
        restauracion / "FacturasProveedores" / "2026" / "OCTUBRE" / "20612414328" / "E001-00000278"
    )
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


# --------------------------------------------------------------------------- #
# correo.txt: la constancia de que la compra llegó por el buzón
# --------------------------------------------------------------------------- #
RECIBIDO = datetime(2026, 10, 25, 12, 57, 48, tzinfo=UTC)  # 07:57 en Lima


def _con_correo(compra: Comprobante) -> Comprobante:
    return compra.model_copy(
        update={
            "correo_asunto": "TRANSPORTE GRUPO AR&J FACTURA E001-278",
            "correo_remitente": "Jenifer Jimena",
            "correo_recibido_at": RECIBIDO,
        }
    )


def _carpeta_278(base: Path) -> Path:
    return base / "FacturasProveedores" / "2026" / "OCTUBRE" / "20612414328" / "E001-00000278"


def test_el_texto_del_correo_trae_asunto_remitente_hora_de_lima_y_huellas(
    uow: FakeUnitOfWork, empresa_principal: Company, almacenamiento: FakeStorageProvider
) -> None:
    compra = _con_correo(_compra_con_archivos(uow, empresa_principal, almacenamiento))

    texto = texto_correo_de_compra(compra, uow.archivos.archivos, mensaje_original="_correos/x.msg")

    assert texto is not None
    assert "Asunto:    TRANSPORTE GRUPO AR&J FACTURA E001-278" in texto
    assert "De:        Jenifer Jimena" in texto
    assert "Recibido:  2026-10-25 07:57:48 (hora de Lima)" in texto
    assert "doc.pdf (10 bytes, sha256 aaaaaaaaaaaaaaaa…)" in texto
    assert "Correo completo (.msg): _correos/x.msg" in texto


def test_sin_correo_no_hay_texto(
    uow: FakeUnitOfWork, empresa_principal: Company, almacenamiento: FakeStorageProvider
) -> None:
    compra = _compra_con_archivos(uow, empresa_principal, almacenamiento)

    assert texto_correo_de_compra(compra, []) is None


def test_al_abrir_una_compra_con_correo_se_escribe_su_correo_txt(
    client: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    empresa_principal: Company,
    almacenamiento: FakeStorageProvider,
    restauracion: Path,
    abiertas: list[Path],
) -> None:
    compra = _con_correo(_compra_con_archivos(uow, empresa_principal, almacenamiento))
    uow.comprobantes.comprobantes[compra.id] = compra

    r = client.post(RESTAURAR, headers=auth, json={"comprobante_id": str(compra.id)})

    assert r.status_code == 200
    texto = (_carpeta_278(restauracion) / "correo.txt").read_text(encoding="utf-8")
    assert "FACTURA E001-278" in texto and "doc.pdf" in texto


def test_no_se_pisa_un_correo_txt_existente_del_extractor(
    client: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    empresa_principal: Company,
    almacenamiento: FakeStorageProvider,
    restauracion: Path,
    abiertas: list[Path],
) -> None:
    compra = _con_correo(_compra_con_archivos(uow, empresa_principal, almacenamiento))
    uow.comprobantes.comprobantes[compra.id] = compra
    carpeta = _carpeta_278(restauracion)
    carpeta.mkdir(parents=True)
    (carpeta / "correo.txt").write_text("con el cuerpo del correo", encoding="utf-8")

    client.post(RESTAURAR, headers=auth, json={"comprobante_id": str(compra.id)})

    assert (carpeta / "correo.txt").read_text(encoding="utf-8") == "con el cuerpo del correo"


def test_una_venta_no_recibe_correo_txt(
    client: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    empresa_principal: Company,
    almacenamiento: FakeStorageProvider,
    restauracion: Path,
    abiertas: list[Path],
) -> None:
    compra = _con_correo(_compra_con_archivos(uow, empresa_principal, almacenamiento))
    venta = compra.model_copy(update={"origen": OrigenComprobante.emitido})
    uow.comprobantes.comprobantes[venta.id] = venta

    r = client.post(RESTAURAR, headers=auth, json={"comprobante_id": str(venta.id)})

    assert r.status_code == 200
    assert not list(restauracion.rglob("correo.txt"))


# --------------------------------------------------------------------------- #
# Paso masivo: toda compra con correo deja carpeta + correo.txt
# --------------------------------------------------------------------------- #
def _fabrica(uow: FakeUnitOfWork):
    @asynccontextmanager
    async def fabrica(_claims):
        yield uow

    return fabrica


def _completar(uow, empresa, settings, almacenamiento, **extra):
    return asyncio.run(
        completar_evidencia_de_correos(
            uow_factory=_fabrica(uow),
            claims={},
            company_id=empresa.id,
            settings=settings,
            storage_provider=almacenamiento,
            **extra,
        )
    )


def test_el_paso_masivo_completa_las_compras_con_correo_y_es_idempotente(
    uow: FakeUnitOfWork,
    empresa_principal: Company,
    settings: Settings,
    almacenamiento: FakeStorageProvider,
    tmp_path: Path,
) -> None:
    settings.local_proveedores_dir = tmp_path / "FacturasProveedores"
    compra = _con_correo(_compra_con_archivos(uow, empresa_principal, almacenamiento))
    uow.comprobantes.comprobantes[compra.id] = compra
    # Una compra sin correo (SIRE solamente) no se toca.
    otra = construir_comprobante(
        empresa_principal.id, serie="F001", correlativo=9, origen=OrigenComprobante.recibido
    )
    uow.comprobantes.comprobantes[otra.id] = otra

    simulado = _completar(uow, empresa_principal, settings, almacenamiento, simular=True)
    assert (simulado["compras_con_correo"], simulado["pendientes"]) == (1, 1)
    assert not list(tmp_path.rglob("*.txt"))

    primero = _completar(uow, empresa_principal, settings, almacenamiento)
    segundo = _completar(uow, empresa_principal, settings, almacenamiento)

    assert primero["completadas"] == 1 and primero["errores"] == []
    assert (_carpeta_278(tmp_path) / "doc.pdf").is_file()
    assert (_carpeta_278(tmp_path) / "correo.txt").is_file()
    assert (segundo["ya_completas"], segundo["completadas"]) == (1, 0)


def test_un_fallo_del_bucket_se_informa_y_no_frena_el_resto(
    uow: FakeUnitOfWork,
    empresa_principal: Company,
    settings: Settings,
    almacenamiento: FakeStorageProvider,
    tmp_path: Path,
) -> None:
    settings.local_proveedores_dir = tmp_path / "FacturasProveedores"
    compra = _con_correo(_compra_con_archivos(uow, empresa_principal, almacenamiento))
    uow.comprobantes.comprobantes[compra.id] = compra
    almacenamiento.objetos.clear()  # el bucket ya no tiene el objeto

    informe = _completar(uow, empresa_principal, settings, almacenamiento)

    assert informe["completadas"] == 0
    assert informe["errores"] == ["E001-00000278: KeyError"]


def test_el_correo_txt_del_portal_se_actualiza_cuando_se_respalda_el_msg(
    uow: FakeUnitOfWork,
    empresa_principal: Company,
    settings: Settings,
    almacenamiento: FakeStorageProvider,
    tmp_path: Path,
) -> None:
    settings.local_proveedores_dir = tmp_path / "FacturasProveedores"
    compra = _con_correo(_compra_con_archivos(uow, empresa_principal, almacenamiento))
    uow.comprobantes.comprobantes[compra.id] = compra
    _completar(uow, empresa_principal, settings, almacenamiento)
    txt = _carpeta_278(tmp_path) / "correo.txt"
    assert "(.msg)" not in txt.read_text(encoding="utf-8")

    # Luego el extractor respalda el correo: JSON con su .msg (hora local de Lima, sin zona).
    destino = tmp_path / "FacturasProveedores"
    (destino / "_correos" / "2026-10").mkdir(parents=True)
    (destino / "_correos" / "2026-10" / "a.msg").write_bytes(b"msg")
    (destino / "_correos" / "2026-10" / "a.json").write_text(
        json.dumps(
            {
                "recibido": "2026-10-25T07:57:48",
                "asunto": "TRANSPORTE  GRUPO AR&J FACTURA E001-278",
                "mensaje_original": {"ruta": "_correos/2026-10/a.msg"},
            }
        ),
        encoding="utf-8",
    )

    informe = _completar(uow, empresa_principal, settings, almacenamiento)

    assert informe["completadas"] == 1
    assert "Correo completo (.msg): _correos/2026-10/a.msg" in txt.read_text(encoding="utf-8")
    assert _completar(uow, empresa_principal, settings, almacenamiento)["completadas"] == 0


def test_un_correo_txt_del_extractor_con_cuerpo_nunca_se_reemplaza_por_el_del_portal(
    uow: FakeUnitOfWork,
    empresa_principal: Company,
    settings: Settings,
    almacenamiento: FakeStorageProvider,
    tmp_path: Path,
) -> None:
    settings.local_proveedores_dir = tmp_path / "FacturasProveedores"
    compra = _con_correo(_compra_con_archivos(uow, empresa_principal, almacenamiento))
    uow.comprobantes.comprobantes[compra.id] = compra
    carpeta = _carpeta_278(tmp_path)
    carpeta.mkdir(parents=True)
    (carpeta / "correo.txt").write_text("Asunto: x\n\nCuerpo:\nhola", encoding="utf-8")
    destino = tmp_path / "FacturasProveedores"
    (destino / "_correos").mkdir()
    (destino / "_correos" / "a.msg").write_bytes(b"msg")
    (destino / "_correos" / "a.json").write_text(
        json.dumps(
            {
                "recibido": "2026-10-25T07:57:48",
                "asunto": "TRANSPORTE GRUPO AR&J FACTURA E001-278",
                "mensaje_original": {"ruta": "_correos/a.msg"},
            }
        ),
        encoding="utf-8",
    )

    informe = _completar(uow, empresa_principal, settings, almacenamiento)

    assert informe["pendientes"] == 0
    assert (carpeta / "correo.txt").read_text(encoding="utf-8").endswith("hola")
