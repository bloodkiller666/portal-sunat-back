"""Carga masiva de CDR por ZIP y los indicadores de respaldo (XML/CDR/PDF) del listado."""

from __future__ import annotations

import hashlib
import io
import zipfile
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.enums import EstadoComprobante, SunatAmbiente, TipoArchivo
from app.db.models import ArchivoRegistro, Company
from tests.conftest import RUC_AJENA, RUC_PRINCIPAL
from tests.fakes import (
    FakeUnitOfWork,
    cargar_cdr_xml,
    construir_comprobante,
    empaquetar_cdr_zip,
)

URL = f"/api/v1/companies/{RUC_PRINCIPAL}/comprobantes/import-cdr"
LISTA = f"/api/v1/companies/{RUC_PRINCIPAL}/comprobantes"


def _zip(archivos: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for nombre, contenido in archivos.items():
            zf.writestr(nombre, contenido)
    return buffer.getvalue()


def _cdr_zip(nombre_xml: str = "R-20123456789-01-F001-00000001.xml") -> bytes:
    """El ZIP que entrega el emisor/OSE: contiene el ApplicationResponse."""
    xml = cargar_cdr_xml("cdr_aceptado.xml")
    return empaquetar_cdr_zip(xml)


def _subir(client: TestClient, headers: dict[str, str], paquete: bytes):
    return client.post(URL, headers=headers, files={"paquete": ("cdrs.zip", paquete)})


@pytest.fixture
def factura(empresa_principal: Company, uow: FakeUnitOfWork):
    creada = construir_comprobante(
        empresa_principal.id,
        ruc=RUC_PRINCIPAL,
        serie="F001",
        correlativo=1,
        ambiente=SunatAmbiente.prod,
        estado=EstadoComprobante.pendiente,
    )
    uow.comprobantes.comprobantes[creada.id] = creada
    return creada


# --------------------------------------------------------------------------- #
# Carga masiva de CDR
# --------------------------------------------------------------------------- #
def test_el_cdr_se_asocia_por_nombre_se_archiva_y_actualiza_el_estado(
    cliente_r2: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, almacenamiento, factura
) -> None:
    cdr = _cdr_zip()
    paquete = _zip({f"R-{RUC_PRINCIPAL}-01-F001-00000001.zip": cdr})

    respuesta = _subir(cliente_r2, auth, paquete)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert (cuerpo["total"], cuerpo["asociados"], cuerpo["ya_tenian"]) == (1, 1, 0)
    item = cuerpo["items"][0]
    assert item["ok"] and item["comprobante_id"] == str(factura.id)
    assert item["estado"] == "aprobado"
    archivado = [a for a in uow.archivos.archivos if a.tipo is TipoArchivo.cdr]
    assert len(archivado) == 1
    assert archivado[0].comprobante_id == factura.id
    assert archivado[0].hash_sha256 == hashlib.sha256(cdr).hexdigest()
    actual = uow.comprobantes.comprobantes[factura.id]
    assert actual.estado is EstadoComprobante.aprobado
    assert actual.cdr_codigo == "0"


def test_tambien_acepta_el_xml_del_cdr_suelto(
    cliente_r2: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, factura
) -> None:
    xml = cargar_cdr_xml("cdr_aceptado.xml").encode()
    paquete = _zip({f"R-{RUC_PRINCIPAL}-01-F001-00000001.xml": xml})

    cuerpo = _subir(cliente_r2, auth, paquete).json()

    assert cuerpo["asociados"] == 1
    # Se archiva siempre como ZIP, igual que lo entrega SUNAT.
    archivado = next(a for a in uow.archivos.archivos if a.tipo is TipoArchivo.cdr)
    assert archivado.nombre.endswith(".zip")


def test_un_cdr_que_ya_existe_no_se_pisa(
    cliente_r2: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, factura
) -> None:
    paquete = _zip({f"R-{RUC_PRINCIPAL}-01-F001-00000001.zip": _cdr_zip()})
    _subir(cliente_r2, auth, paquete)

    segunda = _subir(cliente_r2, auth, paquete).json()

    assert (segunda["asociados"], segunda["ya_tenian"]) == (0, 1)
    assert len([a for a in uow.archivos.archivos if a.tipo is TipoArchivo.cdr]) == 1


def test_cdr_de_un_comprobante_que_no_existe(
    cliente_r2: TestClient, auth: dict[str, str]
) -> None:
    paquete = _zip({f"R-{RUC_PRINCIPAL}-01-F001-00000099.zip": _cdr_zip()})

    cuerpo = _subir(cliente_r2, auth, paquete).json()

    assert cuerpo["sin_comprobante"] == 1
    assert cuerpo["items"][0]["code"] == "sin_comprobante"


def test_cdr_de_otro_ruc_se_rechaza(
    cliente_r2: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, factura
) -> None:
    paquete = _zip({f"R-{RUC_AJENA}-01-F001-00000001.zip": _cdr_zip()})

    cuerpo = _subir(cliente_r2, auth, paquete).json()

    assert cuerpo["con_error"] == 1
    assert cuerpo["items"][0]["code"] == "ruc_distinto"
    assert not uow.archivos.archivos


@pytest.mark.parametrize("nombre", ["cdr.zip", "R-123-01-F001-1.zip", "factura.pdf", "R-x.xml"])
def test_nombres_que_no_son_de_cdr_se_informan(
    cliente_r2: TestClient, auth: dict[str, str], factura, nombre: str
) -> None:
    cuerpo = _subir(cliente_r2, auth, _zip({nombre: b"contenido"})).json()

    assert cuerpo["items"][0]["code"] == "nombre_invalido"


def test_un_cdr_corrupto_no_detiene_a_los_demas(
    cliente_r2: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    empresa_principal: Company,
    factura,
) -> None:
    segunda = construir_comprobante(
        empresa_principal.id, ruc=RUC_PRINCIPAL, serie="F001", correlativo=2,
        ambiente=SunatAmbiente.prod,
    )
    uow.comprobantes.comprobantes[segunda.id] = segunda
    paquete = _zip(
        {
            f"R-{RUC_PRINCIPAL}-01-F001-00000001.zip": b"PK-esto-esta-roto",
            f"R-{RUC_PRINCIPAL}-01-F001-00000002.zip": _cdr_zip(),
        }
    )

    cuerpo = _subir(cliente_r2, auth, paquete).json()

    assert (cuerpo["total"], cuerpo["asociados"], cuerpo["con_error"]) == (2, 1, 1)
    assert uow.comprobantes.comprobantes[factura.id].estado is EstadoComprobante.pendiente


def test_un_cdr_de_rechazo_marca_rechazado(
    cliente_r2: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, factura
) -> None:
    rechazado = empaquetar_cdr_zip(cargar_cdr_xml("cdr_rechazado.xml"))
    paquete = _zip({f"R-{RUC_PRINCIPAL}-01-F001-00000001.zip": rechazado})

    cuerpo = _subir(cliente_r2, auth, paquete).json()

    assert cuerpo["items"][0]["estado"] == "rechazado"


def test_un_aprobado_no_se_degrada_con_un_cdr_posterior(
    cliente_r2: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, factura
) -> None:
    uow.comprobantes.comprobantes[factura.id] = factura.model_copy(
        update={"estado": EstadoComprobante.aprobado}
    )
    rechazado = empaquetar_cdr_zip(cargar_cdr_xml("cdr_rechazado.xml"))

    _subir(cliente_r2, auth, _zip({f"R-{RUC_PRINCIPAL}-01-F001-00000001.zip": rechazado}))

    assert uow.comprobantes.comprobantes[factura.id].estado is EstadoComprobante.aprobado


def test_sin_r2_no_se_cambia_el_estado_a_ciegas(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, factura
) -> None:
    paquete = _zip({f"R-{RUC_PRINCIPAL}-01-F001-00000001.zip": _cdr_zip()})

    cuerpo = _subir(client, auth, paquete).json()

    assert cuerpo["items"][0]["code"] == "no_archivado"
    assert uow.comprobantes.comprobantes[factura.id].estado is EstadoComprobante.pendiente


def test_permisos_y_validaciones_del_endpoint(
    cliente_r2: TestClient, auth: dict[str, str], auth_lector: dict[str, str]
) -> None:
    paquete = _zip({f"R-{RUC_PRINCIPAL}-01-F001-00000001.zip": _cdr_zip()})

    assert _subir(cliente_r2, auth_lector, paquete).status_code == 403
    assert cliente_r2.post(URL, files={"paquete": ("c.zip", paquete)}).status_code == 401
    assert _subir(cliente_r2, auth, b"no es zip").status_code == 422
    assert _subir(cliente_r2, auth, _zip({})).status_code == 422
    assert _subir(cliente_r2, auth, b"").status_code == 422


def test_el_zip_demasiado_grande_se_rechaza(
    cliente_r2: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.cdr_paquete.MAX_BYTES_POR_ENTRADA", 10)
    paquete = _zip({f"R-{RUC_PRINCIPAL}-01-F001-00000001.zip": b"x" * 50})

    assert _subir(cliente_r2, auth, paquete).status_code == 413


def test_el_cdr_busca_primero_el_comprobante_de_produccion(
    cliente_r2: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    empresa_principal: Company,
) -> None:
    beta = construir_comprobante(
        empresa_principal.id, ruc=RUC_PRINCIPAL, serie="F001", correlativo=1,
        ambiente=SunatAmbiente.beta,
    )
    prod = construir_comprobante(
        empresa_principal.id, ruc=RUC_PRINCIPAL, serie="F001", correlativo=1,
        ambiente=SunatAmbiente.prod,
    )
    uow.comprobantes.comprobantes[beta.id] = beta
    uow.comprobantes.comprobantes[prod.id] = prod

    cuerpo = _subir(
        cliente_r2, auth, _zip({f"R-{RUC_PRINCIPAL}-01-F001-00000001.zip": _cdr_zip()})
    ).json()

    assert cuerpo["items"][0]["comprobante_id"] == str(prod.id)


# --------------------------------------------------------------------------- #
# Indicadores XML / CDR / PDF en el listado y el detalle
# --------------------------------------------------------------------------- #
def _archivo(uow: FakeUnitOfWork, comprobante, tipo: TipoArchivo) -> None:
    from datetime import UTC, datetime

    from app.core.enums import EstadoArchivo
    from app.db.models import Archivo

    uow.archivos.archivos.append(
        Archivo(
            id=uuid4(),
            subido_at=datetime.now(tz=UTC),
            **ArchivoRegistro(
                company_id=comprobante.company_id,
                comprobante_id=comprobante.id,
                tipo=tipo,
                nombre=f"x.{tipo.value}",
                estado=EstadoArchivo.subido,
            ).model_dump(),
        )
    )


def test_el_listado_indica_que_respaldos_tiene_cada_comprobante(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    completo = construir_comprobante(empresa_principal.id, ruc=RUC_PRINCIPAL, correlativo=1)
    solo_xml = construir_comprobante(empresa_principal.id, ruc=RUC_PRINCIPAL, correlativo=2)
    vacio = construir_comprobante(empresa_principal.id, ruc=RUC_PRINCIPAL, correlativo=3)
    for c in (completo, solo_xml, vacio):
        uow.comprobantes.comprobantes[c.id] = c
    for tipo in (TipoArchivo.xml, TipoArchivo.cdr, TipoArchivo.pdf):
        _archivo(uow, completo, tipo)
    _archivo(uow, solo_xml, TipoArchivo.xml)

    items = {i["correlativo"]: i for i in client.get(LISTA, headers=auth).json()["items"]}

    assert (items[1]["tiene_xml"], items[1]["tiene_cdr"], items[1]["tiene_pdf"]) == (True,) * 3
    flags2 = (items[2]["tiene_xml"], items[2]["tiene_cdr"], items[2]["tiene_pdf"])
    assert flags2 == (True, False, False)
    assert (items[3]["tiene_xml"], items[3]["tiene_cdr"], items[3]["tiene_pdf"]) == (False,) * 3


def test_el_detalle_tambien_trae_los_indicadores(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    c = construir_comprobante(empresa_principal.id, ruc=RUC_PRINCIPAL, correlativo=1)
    uow.comprobantes.comprobantes[c.id] = c
    _archivo(uow, c, TipoArchivo.cdr)

    cuerpo = client.get(f"/api/v1/comprobantes/{c.id}", headers=auth).json()

    assert (cuerpo["tiene_xml"], cuerpo["tiene_cdr"], cuerpo["tiene_pdf"]) == (False, True, False)
