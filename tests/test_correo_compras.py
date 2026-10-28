"""Columna «Correo» de Compras: de qué correo llegó cada compra."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from scripts import automatizar as au
from scripts.automatizar import Rutas
from scripts.cargar_correos import (
    CorreoDePdfSuelto,
    leer_correos_de_pdf_sueltos,
    leer_registro_correos,
)

from app.core.config import Settings
from app.core.enums import OrigenComprobante, RolEmpresa, SunatAmbiente
from app.core.security import AuthenticatedUser
from app.db.models import Company, CompanyMember
from app.services.correo_compras import CorreoDeCompra, clave_de_carpeta, registrar_correos
from tests.conftest import RUC_PRINCIPAL
from tests.fakes import FakeUnitOfWork, construir_comprobante
from tests.test_api_proveedores_paquete import _pdf_con_texto

UBL = Path(__file__).parent / "fixtures" / "ubl"
FACTURA = (UBL / "factura_recibida.xml").read_bytes()  # F010-00000456 de 20999999999
CARPETA = "2026/10/20999999999/F010-00000456"
PRIMERO = datetime(2026, 10, 20, 9, 30)
DESPUES = datetime(2026, 10, 22, 16, 0)


# --------------------------------------------------------------------------- #
# Carpeta → compra
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("carpeta", "esperado"),
    [
        (CARPETA, ("20999999999", "F010", 456)),
        ("2026/10/20999999999/f010-456", ("20999999999", "F010", 456)),
        ("2026\\10\\20999999999\\E001-00000007", ("20999999999", "E001", 7)),
        ("_sin_clasificar/2026-10/20261021-160605_abcdef", None),
        ("_otras_empresas/20555555555/2026/10/20999999999/F010-00000456", None),
        ("2026/10/999/F010-00000456", None),  # un RUC no puede tener 3 dígitos
        ("2026/10/20999999999/sin-numero", None),
        ("", None),
    ],
)
def test_la_carpeta_identifica_la_compra(carpeta: str, esperado) -> None:
    assert clave_de_carpeta(carpeta) == esperado


# --------------------------------------------------------------------------- #
# Lectura de lo que dejó el extractor (_correos/)
# --------------------------------------------------------------------------- #
def _registro(
    destino: Path,
    nombre: str,
    recibido: datetime,
    asunto: str,
    rutas: list[str],
    remitente: str = "Proveedor <v@p.pe>",
) -> None:
    ruta = destino / "_correos" / f"{recibido:%Y-%m}" / f"{nombre}.json"
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(
        json.dumps(
            {
                "id": nombre,
                "recibido": recibido.isoformat(),
                "asunto": asunto,
                "remitente": remitente,
                "archivos": [{"ruta": r} for r in rutas],
            }
        ),
        encoding="utf-8",
    )


def test_se_lee_un_correo_por_compra_y_gana_el_mas_antiguo(tmp_path: Path) -> None:
    _registro(tmp_path, "b", DESPUES, "Reenvío factura", [f"{CARPETA}/f.xml", f"{CARPETA}/f.pdf"])
    _registro(tmp_path, "a", PRIMERO, "Factura F010-456", [f"{CARPETA}/f.xml"])

    correos = leer_registro_correos(tmp_path)

    (correo,) = correos.values()
    assert (correo.asunto, correo.recibido_at) == ("Factura F010-456", PRIMERO)
    assert correo.ruc_emisor == "20999999999" and correo.correlativo == 456


def test_lo_que_no_es_una_compra_de_la_empresa_se_ignora(tmp_path: Path) -> None:
    _registro(
        tmp_path,
        "x",
        PRIMERO,
        "pdf suelto",
        [
            "_sin_clasificar/2026-10/20261021-160605_abcdef/a.pdf",
            "_otras_empresas/20555555555/2026/10/20999999999/F010-00000456/f.xml",
            "_originales_zip/2026-10/x/lote.zip",
        ],
    )

    assert leer_registro_correos(tmp_path) == {}


def test_un_registro_dañado_no_detiene_la_lectura(tmp_path: Path) -> None:
    (tmp_path / "_correos" / "2026-10").mkdir(parents=True)
    (tmp_path / "_correos" / "2026-10" / "roto.json").write_text("{no es json", encoding="utf-8")
    (tmp_path / "_correos" / "2026-10" / "sin_fecha.json").write_text(
        json.dumps({"asunto": "x", "archivos": []}), encoding="utf-8"
    )
    _registro(tmp_path, "ok", PRIMERO, "Bueno", [f"{CARPETA}/f.xml"])

    assert [c.asunto for c in leer_registro_correos(tmp_path).values()] == ["Bueno"]


def test_sin_carpeta_de_correos_no_hay_nada_que_leer(tmp_path: Path) -> None:
    assert leer_registro_correos(tmp_path) == {}


def test_se_puede_limitar_a_ciertas_carpetas(tmp_path: Path) -> None:
    otra = "2026/10/20999999999/F010-00000999"
    _registro(tmp_path, "a", PRIMERO, "Uno", [f"{CARPETA}/f.xml"])
    _registro(tmp_path, "b", PRIMERO, "Dos", [f"{otra}/f.xml"])

    assert [c.asunto for c in leer_registro_correos(tmp_path, {otra}).values()] == ["Dos"]


# --------------------------------------------------------------------------- #
# Aplicación a la base
# --------------------------------------------------------------------------- #
def _compra(uow: FakeUnitOfWork, empresa: Company, **campos):
    valores = {
        "ruc": "20999999999",
        "serie": "F010",
        "correlativo": 456,
        "origen": OrigenComprobante.recibido,
        "ambiente": SunatAmbiente.prod,
    }
    valores.update(campos)
    compra = construir_comprobante(empresa.id, **valores)
    uow.comprobantes.comprobantes[compra.id] = compra
    return compra


def _acceso(empresa: Company) -> CompanyMember:
    return CompanyMember(company=empresa, rol=RolEmpresa.emisor)


def _correo(asunto: str = "Factura F010-456", recibido: datetime = PRIMERO, **campos):
    valores = {
        "ruc_emisor": "20999999999",
        "serie": "F010",
        "correlativo": 456,
        "asunto": asunto,
        "remitente": "Proveedor <v@p.pe>",
        "recibido_at": recibido,
    }
    valores.update(campos)
    return CorreoDeCompra(**valores)


def _aplicar(uow: FakeUnitOfWork, empresa: Company, *correos: CorreoDeCompra) -> int:
    return asyncio.run(registrar_correos(uow=uow, access=_acceso(empresa), correos=list(correos)))


def test_se_anota_el_correo_en_la_compra(uow: FakeUnitOfWork, empresa_principal: Company) -> None:
    compra = _compra(uow, empresa_principal)

    assert _aplicar(uow, empresa_principal, _correo()) == 1

    actualizada = uow.comprobantes.comprobantes[compra.id]
    assert actualizada.correo_asunto == "Factura F010-456"
    assert actualizada.correo_remitente == "Proveedor <v@p.pe>"
    assert actualizada.correo_recibido_at == PRIMERO


def test_repetir_la_carga_no_cambia_nada(uow: FakeUnitOfWork, empresa_principal: Company) -> None:
    _compra(uow, empresa_principal)
    _aplicar(uow, empresa_principal, _correo())

    assert _aplicar(uow, empresa_principal, _correo()) == 0


def test_un_correo_posterior_no_pisa_al_primero(
    uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    compra = _compra(uow, empresa_principal)
    _aplicar(uow, empresa_principal, _correo("Primero", PRIMERO))

    _aplicar(uow, empresa_principal, _correo("Reenvío", DESPUES))

    assert uow.comprobantes.comprobantes[compra.id].correo_asunto == "Primero"


def test_un_correo_mas_antiguo_si_reemplaza_al_registrado(
    uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    compra = _compra(uow, empresa_principal)
    _aplicar(uow, empresa_principal, _correo("Reenvío", DESPUES))

    _aplicar(uow, empresa_principal, _correo("Original", PRIMERO))

    assert uow.comprobantes.comprobantes[compra.id].correo_asunto == "Original"


def test_la_misma_serie_numero_de_otro_proveedor_no_se_toca(
    uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    ajena = _compra(uow, empresa_principal, ruc="20111111111")

    assert _aplicar(uow, empresa_principal, _correo()) == 0
    assert uow.comprobantes.comprobantes[ajena.id].correo_asunto is None


def test_una_venta_con_esa_serie_numero_no_recibe_el_correo(
    uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    venta = _compra(uow, empresa_principal, origen=OrigenComprobante.emitido)

    assert _aplicar(uow, empresa_principal, _correo()) == 0
    assert uow.comprobantes.comprobantes[venta.id].correo_asunto is None


def test_un_correo_sin_asunto_no_se_anota(uow: FakeUnitOfWork, empresa_principal: Company) -> None:
    compra = _compra(uow, empresa_principal)

    assert _aplicar(uow, empresa_principal, _correo("   ")) == 0
    assert uow.comprobantes.comprobantes[compra.id].correo_asunto is None


def test_el_asunto_se_limpia_y_se_recorta(uow: FakeUnitOfWork, empresa_principal: Company) -> None:
    compra = _compra(uow, empresa_principal)

    _aplicar(uow, empresa_principal, _correo("  Factura\n  F010-456\t octubre " + "x" * 800))

    asunto = uow.comprobantes.comprobantes[compra.id].correo_asunto
    assert asunto.startswith("Factura F010-456 octubre x")
    assert len(asunto) == 500


# --------------------------------------------------------------------------- #
# API: la columna llega al frontend y se puede buscar
# --------------------------------------------------------------------------- #
LISTA = f"/api/v1/companies/{RUC_PRINCIPAL}/comprobantes"


def test_el_listado_de_compras_trae_el_correo_y_se_busca_por_asunto(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    _compra(uow, empresa_principal)
    _compra(uow, empresa_principal, correlativo=457)
    _aplicar(uow, empresa_principal, _correo("Factura de asfalto octubre"))

    todo = client.get(LISTA, headers=auth, params={"origen": "recibido"}).json()
    buscado = client.get(
        LISTA, headers=auth, params={"origen": "recibido", "q": "asfalto"}
    ).json()

    con_correo = [i for i in todo["items"] if i["correo_asunto"]]
    assert [i["correo_asunto"] for i in con_correo] == ["Factura de asfalto octubre"]
    assert con_correo[0]["correo_remitente"] == "Proveedor <v@p.pe>"
    assert [i["correlativo"] for i in buscado["items"]] == [456]


def test_las_ventas_no_traen_correo(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    _compra(uow, empresa_principal, origen=OrigenComprobante.emitido, ruc=RUC_PRINCIPAL)

    (item,) = client.get(LISTA, headers=auth).json()["items"]

    assert item["correo_asunto"] is None and item["correo_recibido_at"] is None


# --------------------------------------------------------------------------- #
# Automatización: al importar una carpeta se anota su correo
# --------------------------------------------------------------------------- #
@pytest.fixture
def entorno(uow: FakeUnitOfWork, empresa_principal: Company, settings: Settings, tmp_path: Path):
    @asynccontextmanager
    async def fabrica(_claims):
        yield uow

    return {
        "fabrica": fabrica,
        "settings": settings,
        "usuario": AuthenticatedUser(id=uuid4()),
        "acceso": _acceso(empresa_principal),
        "destino": tmp_path,
        "rutas": Rutas(tmp_path / "_automatizacion"),
    }


def _importar(entorno):
    return asyncio.run(
        au.importar_pendientes(
            entorno["fabrica"],
            entorno["settings"],
            entorno["usuario"],
            entorno["acceso"],
            entorno["destino"],
            entorno["rutas"],
        )
    )


def _preparar(entorno, asunto: str | None = "Factura F010-456") -> None:
    carpeta = entorno["destino"] / CARPETA
    carpeta.mkdir(parents=True)
    (carpeta / "f.xml").write_bytes(FACTURA)
    au._escribir_json(entorno["rutas"].pendientes, {"carpetas": [CARPETA], "sin_clasificar": []})
    if asunto is not None:
        _registro(entorno["destino"], "a", PRIMERO, asunto, [f"{CARPETA}/f.xml"])


def test_al_importar_la_carpeta_se_anota_el_correo(entorno, uow: FakeUnitOfWork) -> None:
    _preparar(entorno)

    informe = _importar(entorno)

    assert informe["errores"] == [] and informe["correos_registrados"] == 1
    (compra,) = uow.comprobantes.comprobantes.values()
    assert compra.correo_asunto == "Factura F010-456"
    assert compra.correo_recibido_at == PRIMERO


def test_sin_registro_de_correo_la_importacion_sigue_igual(entorno, uow: FakeUnitOfWork) -> None:
    _preparar(entorno, asunto=None)

    informe = _importar(entorno)

    assert informe["errores"] == [] and informe["correos_registrados"] == 0
    (compra,) = uow.comprobantes.comprobantes.values()
    assert compra.correo_asunto is None


def test_un_fallo_al_anotar_el_correo_se_informa_pero_la_compra_queda_importada(
    entorno, uow: FakeUnitOfWork, monkeypatch: pytest.MonkeyPatch
) -> None:
    _preparar(entorno)

    async def falla(*_a, **_k):
        raise ValueError("base caída")

    monkeypatch.setattr(uow.comprobantes, "registrar_correo", falla)

    informe = _importar(entorno)

    assert any("correo" in e for e in informe["errores"])
    assert len(uow.comprobantes.comprobantes) == 1


# --------------------------------------------------------------------------- #
# PDF que llegó SIN XML: queda en `_sin_clasificar`, su compra se identifica por el texto
# --------------------------------------------------------------------------- #
SUELTO = "_sin_clasificar/2026-10/20261025-075748_179ed5"


def _escribir_pdf_suelto(destino: Path, nombre: str, texto: str) -> str:
    ruta = destino / SUELTO / nombre
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_bytes(_pdf_con_texto(texto))
    return f"{SUELTO}/{nombre}"


def _anotar_sueltos(entorno, correos: list[CorreoDePdfSuelto]) -> tuple[int, dict]:
    informe: dict = {"errores": []}
    anotadas = asyncio.run(
        au.anotar_correos_de_pdf_sueltos(
            entorno["fabrica"],
            entorno["usuario"],
            entorno["acceso"],
            entorno["destino"],
            correos,
            informe,
        )
    )
    return anotadas, informe


def test_se_leen_solo_los_pdf_sin_clasificar_con_su_correo(tmp_path: Path) -> None:
    _registro(
        tmp_path,
        "a",
        PRIMERO,
        "TRANSPORTE FACTURA E001-278",
        [f"{SUELTO}/GRUPO.pdf", f"{SUELTO}/foto.png", f"{CARPETA}/f.pdf", "_otras_empresas/x.pdf"],
    )

    (suelto,) = leer_correos_de_pdf_sueltos(tmp_path)

    assert suelto.ruta == f"{SUELTO}/GRUPO.pdf"
    assert (suelto.asunto, suelto.recibido_at) == ("TRANSPORTE FACTURA E001-278", PRIMERO)
    assert leer_correos_de_pdf_sueltos(tmp_path, {"_sin_clasificar/otra"}) == []


def test_el_pdf_sin_xml_anota_el_correo_en_la_compra_que_su_texto_identifica(
    entorno, uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    compra = _compra(uow, empresa_principal)
    ruta = _escribir_pdf_suelto(
        entorno["destino"], "f.pdf", "Proveedor RUC 20999999999 FACTURA F010-00000456"
    )
    _registro(entorno["destino"], "a", PRIMERO, "Factura F010-456", [ruta])

    anotadas, informe = _anotar_sueltos(entorno, leer_correos_de_pdf_sueltos(entorno["destino"]))

    assert anotadas == 1 and informe["errores"] == []
    actualizada = uow.comprobantes.comprobantes[compra.id]
    assert actualizada.correo_asunto == "Factura F010-456"
    assert actualizada.correo_recibido_at == PRIMERO


def test_un_pdf_que_no_se_identifica_o_es_ambiguo_no_anota_nada(
    entorno, uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    uno = _compra(uow, empresa_principal)
    _compra(uow, empresa_principal, ruc="20888888888")  # misma serie-número, otro proveedor
    rutas = [
        _escribir_pdf_suelto(entorno["destino"], "orden.pdf", "Orden de compra 4500055493"),
        _escribir_pdf_suelto(
            entorno["destino"],
            "ambiguo.pdf",
            "RUC 20999999999 RUC 20888888888 FACTURA F010-00000456",
        ),
    ]
    _registro(entorno["destino"], "a", PRIMERO, "Facturas", rutas)

    anotadas, informe = _anotar_sueltos(entorno, leer_correos_de_pdf_sueltos(entorno["destino"]))

    assert anotadas == 0 and informe["errores"] == []
    assert uow.comprobantes.comprobantes[uno.id].correo_asunto is None


def test_un_pdf_borrado_o_con_correo_sin_asunto_se_salta(
    entorno, uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    compra = _compra(uow, empresa_principal)
    texto = "RUC 20999999999 FACTURA F010-00000456"
    sin_asunto = _escribir_pdf_suelto(entorno["destino"], "a.pdf", texto)
    _registro(entorno["destino"], "a", PRIMERO, "  ", [sin_asunto])
    _registro(entorno["destino"], "b", PRIMERO, "Factura", [f"{SUELTO}/no_existe.pdf"])

    anotadas, informe = _anotar_sueltos(entorno, leer_correos_de_pdf_sueltos(entorno["destino"]))

    assert anotadas == 0 and informe["errores"] == []
    assert uow.comprobantes.comprobantes[compra.id].correo_asunto is None


def test_un_fallo_al_anotar_un_pdf_se_informa_y_sigue_con_los_demas(
    entorno, uow: FakeUnitOfWork, empresa_principal: Company, monkeypatch: pytest.MonkeyPatch
) -> None:
    _compra(uow, empresa_principal)
    ruta = _escribir_pdf_suelto(
        entorno["destino"], "f.pdf", "RUC 20999999999 FACTURA F010-00000456"
    )
    _registro(entorno["destino"], "a", PRIMERO, "Factura", [ruta])

    async def falla(*_a, **_k):
        raise ValueError("base caída")

    monkeypatch.setattr(uow.comprobantes, "registrar_correo", falla)

    anotadas, informe = _anotar_sueltos(entorno, leer_correos_de_pdf_sueltos(entorno["destino"]))

    assert anotadas == 0
    assert any("correo" in e for e in informe["errores"])


def test_al_importar_los_pdf_sueltos_pendientes_se_anota_su_correo(
    entorno, uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    compra = _compra(uow, empresa_principal)
    ruta = _escribir_pdf_suelto(
        entorno["destino"], "f.pdf", "RUC 20999999999 FACTURA F010-00000456"
    )
    _registro(entorno["destino"], "a", PRIMERO, "Factura F010-456", [ruta])
    au._escribir_json(entorno["rutas"].pendientes, {"carpetas": [], "sin_clasificar": [SUELTO]})

    informe = _importar(entorno)

    assert informe["errores"] == [] and informe["correos_registrados"] == 1
    assert uow.comprobantes.comprobantes[compra.id].correo_asunto == "Factura F010-456"
