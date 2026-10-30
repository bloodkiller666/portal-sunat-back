"""Pruebas de los endpoints de archivos y de la bitácora de intentos SUNAT."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.enums import EstadoArchivo, TipoArchivo
from app.db.models import Archivo, Company, Comprobante
from tests.conftest import RUC_PRINCIPAL, crear_token
from tests.fakes import (
    FakeSunatClient,
    FakeUnitOfWork,
    construir_comprobante,
    fabricar_cliente_sunat,
    resultado_con_cdr,
)

UBL = Path(__file__).parent / "fixtures" / "ubl"
IMPORT = f"/api/v1/companies/{RUC_PRINCIPAL}/comprobantes/import"


def _archivo(comprobante: Comprobante, *, tipo: TipoArchivo = TipoArchivo.xml) -> Archivo:
    return Archivo(
        id=uuid4(),
        company_id=comprobante.company_id,
        comprobante_id=comprobante.id,
        tipo=tipo,
        nombre=f"{comprobante.nombre_archivo}.xml",
        mime_type="application/xml",
        size_bytes=128,
        hash_sha256="a" * 64,
        storage_provider="r2",
        storage_bucket="bucket-privado",
        storage_key=(
            f"comprobantes/{RUC_PRINCIPAL}/2026/10. OCTUBRE"
            f"/{comprobante.serie}-{comprobante.numero}"
            f"/{comprobante.nombre_archivo}.xml"
        ),
        estado=EstadoArchivo.subido,
        subido_at=datetime.now(tz=UTC),
    )


@pytest.fixture
def comprobante(empresa_principal: Company, uow: FakeUnitOfWork) -> Comprobante:
    creado = construir_comprobante(empresa_principal.id, ruc=RUC_PRINCIPAL, correlativo=1)
    uow.comprobantes.comprobantes[creado.id] = creado
    return creado


# --------------------------------------------------------------------------- #
# Listado de archivos
# --------------------------------------------------------------------------- #
def _ruta_archivos(comprobante_id: UUID) -> str:
    return f"/api/v1/comprobantes/{comprobante_id}/archivos"


def test_listar_archivos_sin_token_devuelve_401(client: TestClient) -> None:
    assert client.get(_ruta_archivos(uuid4())).status_code == 401


def test_listar_archivos_del_comprobante(
    client: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    comprobante: Comprobante,
) -> None:
    archivo = _archivo(comprobante)
    uow.archivos.archivos.append(archivo)

    respuesta = client.get(_ruta_archivos(comprobante.id), headers=auth)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["comprobante_id"] == str(comprobante.id)
    assert cuerpo["total"] == 1
    item = cuerpo["items"][0]
    assert item["id"] == str(archivo.id)
    assert item["tipo"] == "xml"
    assert item["nombre"] == archivo.nombre
    # El listado nunca expone la clave del bucket ni una URL firmada.
    assert "storage_key" not in respuesta.text
    assert "https://" not in respuesta.text


def test_listar_archivos_como_lector_esta_permitido(
    client: TestClient, auth_lector: dict[str, str], comprobante: Comprobante
) -> None:
    respuesta = client.get(_ruta_archivos(comprobante.id), headers=auth_lector)
    assert respuesta.status_code == 200


def test_listar_archivos_de_otra_empresa_no_es_visible(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_ajena: Company
) -> None:
    ajeno = construir_comprobante(empresa_ajena.id, ruc=empresa_ajena.ruc)
    uow.comprobantes.comprobantes[ajeno.id] = ajeno

    assert client.get(_ruta_archivos(ajeno.id), headers=auth).status_code == 404


# --------------------------------------------------------------------------- #
# URL firmada de descarga
# --------------------------------------------------------------------------- #
def _ruta_descarga(archivo_id: UUID) -> str:
    return f"/api/v1/archivos/{archivo_id}/download-url"


def test_descarga_sin_token_devuelve_401(client: TestClient) -> None:
    assert client.post(_ruta_descarga(uuid4())).status_code == 401


def test_descarga_de_archivo_inexistente(client: TestClient, auth: dict[str, str]) -> None:
    assert client.post(_ruta_descarga(uuid4()), headers=auth).status_code == 404


def test_descarga_de_otra_empresa_no_es_visible(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_ajena: Company
) -> None:
    ajeno = construir_comprobante(empresa_ajena.id, ruc=empresa_ajena.ruc)
    archivo = _archivo(ajeno)
    uow.archivos.archivos.append(archivo)

    assert client.post(_ruta_descarga(archivo.id), headers=auth).status_code == 404


def test_sin_r2_configurado_la_descarga_responde_503(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, comprobante: Comprobante
) -> None:
    archivo = _archivo(comprobante)
    uow.archivos.archivos.append(archivo)

    respuesta = client.post(_ruta_descarga(archivo.id), headers=auth)

    assert respuesta.status_code == 503
    assert respuesta.json()["error"]["code"] == "storage_not_configured"


def test_descarga_devuelve_url_firmada_temporal(
    app_r2: FastAPI,
    cliente_r2: TestClient,
    uow: FakeUnitOfWork,
    comprobante: Comprobante,
) -> None:
    archivo = _archivo(comprobante)
    uow.archivos.archivos.append(archivo)
    auth = {"Authorization": f"Bearer {crear_token()}"}

    respuesta = cliente_r2.post(_ruta_descarga(archivo.id), headers=auth)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["archivo_id"] == str(archivo.id)
    assert cuerpo["nombre"] == archivo.nombre
    assert cuerpo["url"] == f"https://firmada/{archivo.storage_key}"
    assert cuerpo["expires_in"] == 900
    expira_at = datetime.fromisoformat(cuerpo["expira_at"].replace("Z", "+00:00"))
    assert expira_at > datetime.now(tz=UTC)


# --------------------------------------------------------------------------- #
# El XML importado y el CDR quedan archivados
# --------------------------------------------------------------------------- #
def test_la_importacion_archiva_el_xml(
    app_r2: FastAPI, cliente_r2: TestClient, uow: FakeUnitOfWork
) -> None:
    auth = {"Authorization": f"Bearer {crear_token()}"}
    xml = (UBL / "factura.xml").read_bytes()

    respuesta = cliente_r2.post(
        IMPORT, headers=auth, files={"archivo": ("factura.xml", xml, "application/xml")}
    )

    assert respuesta.status_code == 201
    comprobante_id = UUID(respuesta.json()["id"])
    guardados = [a for a in uow.archivos.archivos if a.comprobante_id == comprobante_id]
    assert len(guardados) == 1
    assert guardados[0].tipo is TipoArchivo.xml
    assert guardados[0].nombre == f"{RUC_PRINCIPAL}-01-F001-00000001.xml"
    # Ruta pedida: AÑO/MES/N° DOCUMENTO, con el XML dentro de la carpeta del documento.
    assert re.fullmatch(
        rf"comprobantes/{RUC_PRINCIPAL}/\d{{4}}/\d{{1,2}}\. [A-Z]+/F001-00000001/"
        rf"{RUC_PRINCIPAL}-01-F001-00000001\.xml",
        guardados[0].storage_key,
    ), guardados[0].storage_key
    assert guardados[0].mime_type == "application/xml"


def test_el_refresco_archiva_el_cdr_una_sola_vez(
    app_r2: FastAPI, cliente_r2: TestClient, uow: FakeUnitOfWork, comprobante: Comprobante
) -> None:
    app_r2.state.sunat_client_factory = fabricar_cliente_sunat(
        FakeSunatClient(resultado=resultado_con_cdr())
    )
    auth = {"Authorization": f"Bearer {crear_token()}"}
    ruta = f"/api/v1/comprobantes/{comprobante.id}/refresh-cdr"

    assert cliente_r2.post(ruta, headers=auth).status_code == 200
    assert cliente_r2.post(ruta, headers=auth).status_code == 200

    guardados = [a for a in uow.archivos.archivos if a.tipo is TipoArchivo.cdr]
    assert len(guardados) == 1
    assert guardados[0].mime_type == "application/zip"
    assert guardados[0].nombre.endswith("-CDR.zip")
    assert guardados[0].hash_sha256


def test_sin_r2_el_refresco_sigue_funcionando(
    client: TestClient,
    auth: dict[str, str],
    sunat: FakeSunatClient,
    uow: FakeUnitOfWork,
    comprobante: Comprobante,
) -> None:
    """El archivo es un extra: si el bucket no está configurado, todo sigue igual."""
    sunat.resultado = resultado_con_cdr()

    respuesta = client.post(f"/api/v1/comprobantes/{comprobante.id}/refresh-cdr", headers=auth)

    assert respuesta.status_code == 200
    assert respuesta.json()["estado_nuevo"] == "aprobado"
    assert uow.archivos.archivos == []


# --------------------------------------------------------------------------- #
# Bitácora de intentos
# --------------------------------------------------------------------------- #
def _ruta_envios(comprobante_id: UUID) -> str:
    return f"/api/v1/comprobantes/{comprobante_id}/envios"


def test_envios_sin_token_devuelve_401(client: TestClient, comprobante: Comprobante) -> None:
    assert client.get(_ruta_envios(comprobante.id)).status_code == 401


def test_envios_de_comprobante_inexistente(client: TestClient, auth: dict[str, str]) -> None:
    assert client.get(_ruta_envios(uuid4()), headers=auth).status_code == 404


def test_envios_devuelve_los_intentos_del_comprobante(
    client: TestClient,
    auth: dict[str, str],
    sunat: FakeSunatClient,
    comprobante: Comprobante,
) -> None:
    sunat.resultado = resultado_con_cdr()
    client.post(f"/api/v1/comprobantes/{comprobante.id}/refresh-cdr", headers=auth)

    respuesta = client.get(_ruta_envios(comprobante.id), headers=auth)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["comprobante_id"] == str(comprobante.id)
    assert cuerpo["total"] == 1
    intento = cuerpo["items"][0]
    assert intento["tipo_operacion"] == "consulta_cdr"
    assert intento["exito"] is True
    assert intento["codigo"] == "0"
    assert intento["duracion_ms"] == 42
    # Sin tramas SOAP en la respuesta.
    assert "request_xml" not in respuesta.text
    assert "response_xml" not in respuesta.text


def test_envios_como_lector_esta_permitido(
    client: TestClient, auth_lector: dict[str, str], comprobante: Comprobante
) -> None:
    respuesta = client.get(_ruta_envios(comprobante.id), headers=auth_lector)
    assert respuesta.status_code == 200
    assert respuesta.json()["items"] == []


def test_envios_de_otra_empresa_no_es_visible(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_ajena: Company
) -> None:
    ajeno = construir_comprobante(empresa_ajena.id, ruc=empresa_ajena.ruc)
    uow.comprobantes.comprobantes[ajeno.id] = ajeno

    assert client.get(_ruta_envios(ajeno.id), headers=auth).status_code == 404


def test_envios_valida_el_limite(
    client: TestClient, auth: dict[str, str], comprobante: Comprobante
) -> None:
    respuesta = client.get(_ruta_envios(comprobante.id), headers=auth, params={"limit": 0})
    assert respuesta.status_code == 422
