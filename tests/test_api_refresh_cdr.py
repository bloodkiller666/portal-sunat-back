from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.enums import EstadoComprobante, TipoOperacionSunat
from app.core.errors import SunatAuthError, SunatFaultError, SunatUnavailableError
from app.db.models import Company, Comprobante
from tests.conftest import CLAVE_SOL, RUC_PRINCIPAL
from tests.fakes import (
    FakeSunatClient,
    FakeUnitOfWork,
    construir_comprobante,
    resultado_con_cdr,
    resultado_sin_cdr,
)


@pytest.fixture
def comprobante(empresa_principal: Company, uow: FakeUnitOfWork) -> Comprobante:
    creado = construir_comprobante(empresa_principal.id, ruc=RUC_PRINCIPAL, correlativo=1)
    uow.comprobantes.comprobantes[creado.id] = creado
    return creado


def _ruta(comprobante: Comprobante) -> str:
    return f"/api/v1/comprobantes/{comprobante.id}/refresh-cdr"


def test_actualiza_el_estado_con_un_cdr_aprobado(
    client: TestClient,
    auth: dict[str, str],
    sunat: FakeSunatClient,
    uow: FakeUnitOfWork,
    comprobante: Comprobante,
) -> None:
    sunat.resultado = resultado_con_cdr()

    respuesta = client.post(_ruta(comprobante), headers=auth)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["estado_anterior"] == "pendiente"
    assert cuerpo["estado_nuevo"] == "aprobado"
    assert cuerpo["estado_label"] == "Aprobado"
    assert cuerpo["cambio_estado"] is True
    assert cuerpo["cdr"]["codigo"] == "0"
    assert cuerpo["cdr"]["notas"]
    assert cuerpo["sunat_env"] == "beta"

    # La base quedó sincronizada.
    guardado = uow.comprobantes.comprobantes[comprobante.id]
    assert guardado.estado is EstadoComprobante.aprobado
    assert guardado.cdr_codigo == "0"
    assert guardado.ultimo_envio_at is not None

    # Y el intento quedó registrado.
    assert len(uow.sunat_envios.envios) == 1
    intento = uow.sunat_envios.envios[0]
    assert intento.tipo_operacion is TipoOperacionSunat.consulta_cdr
    assert intento.exito is True
    assert intento.codigo_respuesta == "0"
    assert intento.comprobante_id == comprobante.id


def test_cdr_rechazado_marca_rechazado(
    client: TestClient, auth: dict[str, str], sunat: FakeSunatClient, comprobante: Comprobante
) -> None:
    sunat.resultado = resultado_con_cdr(codigo="2025", mensaje="El comprobante fue rechazado")

    cuerpo = client.post(_ruta(comprobante), headers=auth).json()

    assert cuerpo["estado_nuevo"] == "rechazado"
    assert cuerpo["cdr"]["codigo"] == "2025"


def test_codigo_en_proceso_sin_cdr_deja_pendiente(
    client: TestClient, auth: dict[str, str], sunat: FakeSunatClient, comprobante: Comprobante
) -> None:
    sunat.resultado = resultado_sin_cdr("98", "El comprobante está en proceso")

    respuesta = client.post(_ruta(comprobante), headers=auth)
    cuerpo = respuesta.json()

    assert respuesta.status_code == 200
    assert cuerpo["estado_nuevo"] == "pendiente"
    assert cuerpo["cambio_estado"] is False
    assert cuerpo["cdr"] is None
    assert "98" in cuerpo["mensaje"]


def test_sunat_todavia_no_tiene_el_comprobante(
    client: TestClient,
    auth: dict[str, str],
    sunat: FakeSunatClient,
    uow: FakeUnitOfWork,
    comprobante: Comprobante,
) -> None:
    sunat.error = SunatFaultError(
        "El comprobante no existe",
        details={"codigo": "1033", "no_disponible": True},
    )

    respuesta = client.post(_ruta(comprobante), headers=auth)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["estado_nuevo"] == "pendiente"
    assert "no existe" in cuerpo["mensaje"]
    assert cuerpo["intento"]["exito"] is False
    assert cuerpo["intento"]["codigo"] == "1033"
    assert len(uow.sunat_envios.envios) == 1


def test_sunat_no_disponible_devuelve_503_y_registra_el_intento(
    client: TestClient,
    auth: dict[str, str],
    sunat: FakeSunatClient,
    uow: FakeUnitOfWork,
    comprobante: Comprobante,
) -> None:
    sunat.error = SunatUnavailableError("No se pudo contactar a SUNAT")

    respuesta = client.post(_ruta(comprobante), headers=auth)

    assert respuesta.status_code == 503
    assert respuesta.json()["error"]["code"] == "sunat_unavailable"
    assert respuesta.json()["error"]["details"]["intento_id"] == 1
    assert len(uow.sunat_envios.envios) == 1
    assert uow.sunat_envios.envios[0].error_tipo == "sunat_unavailable"
    # El estado no se toca cuando no hay respuesta de SUNAT.
    assert uow.comprobantes.comprobantes[comprobante.id].estado is EstadoComprobante.pendiente


def test_credenciales_rechazadas_devuelve_502(
    client: TestClient, auth: dict[str, str], sunat: FakeSunatClient, comprobante: Comprobante
) -> None:
    sunat.error = SunatAuthError("SUNAT rechazó las credenciales SOL de la empresa")

    respuesta = client.post(_ruta(comprobante), headers=auth)

    assert respuesta.status_code == 502
    assert respuesta.json()["error"]["code"] == "sunat_auth_error"


def test_comprobante_inexistente(client: TestClient, auth: dict[str, str]) -> None:
    respuesta = client.post(f"/api/v1/comprobantes/{uuid4()}/refresh-cdr", headers=auth)
    assert respuesta.status_code == 404


def test_comprobante_de_otra_empresa_no_es_visible(
    client: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    empresa_ajena: Company,
) -> None:
    """La RLS oculta los comprobantes de empresas donde el usuario no es miembro."""
    ajeno = construir_comprobante(empresa_ajena.id, ruc=empresa_ajena.ruc)
    uow.comprobantes.comprobantes[ajeno.id] = ajeno

    respuesta = client.post(_ruta(ajeno), headers=auth)
    assert respuesta.status_code == 404


def test_rol_lector_no_puede_actualizar(
    client: TestClient, auth_lector: dict[str, str], comprobante: Comprobante
) -> None:
    respuesta = client.post(_ruta(comprobante), headers=auth_lector)
    assert respuesta.status_code == 403
    assert respuesta.json()["error"]["details"]["rol"] == "lector"


def test_no_revierte_un_comprobante_anulado(
    client: TestClient,
    auth: dict[str, str],
    sunat: FakeSunatClient,
    uow: FakeUnitOfWork,
    empresa_principal: Company,
) -> None:
    anulado = construir_comprobante(
        empresa_principal.id, ruc=RUC_PRINCIPAL, estado=EstadoComprobante.anulado
    )
    uow.comprobantes.comprobantes[anulado.id] = anulado
    sunat.resultado = resultado_con_cdr()

    cuerpo = client.post(_ruta(anulado), headers=auth).json()

    assert cuerpo["estado_nuevo"] == "anulado"
    assert cuerpo["cambio_estado"] is False
    assert cuerpo["cdr"]["codigo"] == "0"


def test_envia_los_datos_del_comprobante_a_sunat(
    client: TestClient,
    auth: dict[str, str],
    sunat: FakeSunatClient,
    comprobante: Comprobante,
) -> None:
    sunat.resultado = resultado_con_cdr()

    client.post(_ruta(comprobante), headers=auth)

    llamada = sunat.llamadas[0]
    assert llamada["metodo"] == "get_status_cdr"
    assert llamada["tipo_comprobante"] == "01"
    assert llamada["serie"] == "F001"
    assert llamada["numero"] == "00000001"
    # El WSDL real de getStatusCdr no declara montoTotal (lo confirmamos en prod).
    assert "monto_total" not in llamada


def test_la_respuesta_no_expone_tramas_ni_credenciales(
    client: TestClient, auth: dict[str, str], sunat: FakeSunatClient, comprobante: Comprobante
) -> None:
    sunat.resultado = resultado_con_cdr()

    respuesta = client.post(_ruta(comprobante), headers=auth)

    assert "request_xml" not in respuesta.text
    assert "response_xml" not in respuesta.text
    assert CLAVE_SOL not in respuesta.text


def test_el_comprobante_debe_ser_un_uuid(client: TestClient, auth: dict[str, str]) -> None:
    respuesta = client.post("/api/v1/comprobantes/no-es-uuid/refresh-cdr", headers=auth)
    assert respuesta.status_code == 422


# --------------------------------------------------------------------------- #
# Caso real (producción): factura listada en SIRE, consultada con getStatusCdr
# --------------------------------------------------------------------------- #
MENSAJE_0013 = "El comprobante de pago electrónico no pertenece a GEM."


def test_0013_no_degrada_un_comprobante_aprobado_de_sire(
    client: TestClient,
    auth: dict[str, str],
    sunat: FakeSunatClient,
    uow: FakeUnitOfWork,
    empresa_principal: Company,
) -> None:
    desde_sire = construir_comprobante(
        empresa_principal.id, ruc=RUC_PRINCIPAL, estado=EstadoComprobante.aprobado
    )
    uow.comprobantes.comprobantes[desde_sire.id] = desde_sire
    sunat.resultado = resultado_sin_cdr("0013", MENSAJE_0013)

    respuesta = client.post(_ruta(desde_sire), headers=auth)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["estado_nuevo"] == "aprobado"
    assert cuerpo["cambio_estado"] is False
    assert cuerpo["cdr"] is None
    assert "0013" in cuerpo["mensaje"]
    assert uow.comprobantes.comprobantes[desde_sire.id].estado is EstadoComprobante.aprobado


@pytest.mark.parametrize("codigo", ["0011", "0127", "98", "2324"])
def test_ninguna_respuesta_sin_cdr_degrada_un_aprobado(
    client: TestClient,
    auth: dict[str, str],
    sunat: FakeSunatClient,
    uow: FakeUnitOfWork,
    empresa_principal: Company,
    codigo: str,
) -> None:
    aprobado = construir_comprobante(
        empresa_principal.id, ruc=RUC_PRINCIPAL, estado=EstadoComprobante.aprobado
    )
    uow.comprobantes.comprobantes[aprobado.id] = aprobado
    sunat.resultado = resultado_sin_cdr(codigo, "cualquier mensaje")

    cuerpo = client.post(_ruta(aprobado), headers=auth).json()

    assert cuerpo["estado_nuevo"] == "aprobado"


def test_0013_en_un_pendiente_lo_deja_pendiente_y_no_rechazado(
    client: TestClient, auth: dict[str, str], sunat: FakeSunatClient, comprobante: Comprobante
) -> None:
    sunat.resultado = resultado_sin_cdr("0013", MENSAJE_0013)

    cuerpo = client.post(_ruta(comprobante), headers=auth).json()

    assert cuerpo["estado_nuevo"] == "pendiente"
