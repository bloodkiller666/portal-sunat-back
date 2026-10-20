from __future__ import annotations

import io
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.uploads import BLOQUE_LECTURA_BYTES, leer_con_limite
from app.core.enums import EstadoComprobante
from app.core.errors import FileTooLargeError
from app.db.models import Company
from app.services.comprobantes import TAMANO_MAXIMO_XML
from tests.conftest import RUC_AJENA, RUC_PRINCIPAL, USUARIO_ID
from tests.fakes import FakeUnitOfWork, construir_comprobante

UBL = Path(__file__).parent / "fixtures" / "ubl"

LISTA = f"/api/v1/companies/{RUC_PRINCIPAL}/comprobantes"
IMPORT = f"/api/v1/companies/{RUC_PRINCIPAL}/comprobantes/import"


def _xml(nombre: str) -> bytes:
    return (UBL / nombre).read_bytes()


def test_listar_sin_token_devuelve_401(client: TestClient) -> None:
    assert client.get(LISTA).status_code == 401


def test_listar_como_lector_esta_permitido(
    client: TestClient, auth_lector: dict[str, str]
) -> None:
    respuesta = client.get(LISTA, headers=auth_lector)
    assert respuesta.status_code == 200
    assert respuesta.json()["items"] == []


def test_listar_devuelve_los_comprobantes_de_la_empresa(
    client: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    empresa_principal: Company,
) -> None:
    uow.comprobantes.comprobantes[uuid4()] = construir_comprobante(
        empresa_principal.id, ruc=RUC_PRINCIPAL, correlativo=1
    )
    uow.comprobantes.comprobantes[uuid4()] = construir_comprobante(
        empresa_principal.id, ruc=RUC_PRINCIPAL, correlativo=2
    )

    cuerpo = client.get(LISTA, headers=auth).json()

    assert len(cuerpo["items"]) == 2
    assert cuerpo["has_more"] is False
    item = cuerpo["items"][0]
    assert item["estado_label"] == "Pendiente"
    assert item["ruc_emisor"] == RUC_PRINCIPAL


def test_listar_filtra_por_estado(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    uow.comprobantes.comprobantes[uuid4()] = construir_comprobante(
        empresa_principal.id, ruc=RUC_PRINCIPAL, correlativo=1
    )
    uow.comprobantes.comprobantes[uuid4()] = construir_comprobante(
        empresa_principal.id,
        ruc=RUC_PRINCIPAL,
        correlativo=2,
        estado=EstadoComprobante.aprobado,
    )

    cuerpo = client.get(LISTA, headers=auth, params={"estado": "aprobado"}).json()

    assert len(cuerpo["items"]) == 1
    assert cuerpo["items"][0]["estado"] == "aprobado"


def test_listar_paginado_marca_has_more(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    for correlativo in range(1, 4):
        uow.comprobantes.comprobantes[uuid4()] = construir_comprobante(
            empresa_principal.id, ruc=RUC_PRINCIPAL, correlativo=correlativo
        )

    cuerpo = client.get(LISTA, headers=auth, params={"limit": 2}).json()

    assert len(cuerpo["items"]) == 2
    assert cuerpo["has_more"] is True
    assert cuerpo["limit"] == 2


def test_listar_empresa_ajena(client: TestClient, auth: dict[str, str]) -> None:
    respuesta = client.get(f"/api/v1/companies/{RUC_AJENA}/comprobantes", headers=auth)
    assert respuesta.status_code == 403


def test_listar_empresa_inexistente(client: TestClient, auth: dict[str, str]) -> None:
    respuesta = client.get("/api/v1/companies/20111111111/comprobantes", headers=auth)
    assert respuesta.status_code == 404


def test_importar_factura_crea_el_comprobante(
    client: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    empresa_principal: Company,
) -> None:
    respuesta = client.post(
        IMPORT,
        headers=auth,
        files={"archivo": ("factura.xml", _xml("factura.xml"), "application/xml")},
    )

    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    assert cuerpo["tipo_documento"] == "01"
    assert cuerpo["serie"] == "F001"
    assert cuerpo["correlativo"] == 1
    assert cuerpo["numero"] == "00000001"
    assert cuerpo["nombre_archivo"] == f"{RUC_PRINCIPAL}-01-F001-00000001"
    assert cuerpo["moneda"] == "PEN"
    assert cuerpo["monto_total"] == "118.00"
    assert cuerpo["estado"] == "pendiente"
    assert cuerpo["sunat_ambiente"] == "beta"

    guardado = next(iter(uow.comprobantes.comprobantes.values()))
    assert guardado.company_id == empresa_principal.id
    assert guardado.created_by == USUARIO_ID


def test_importar_duplicado_devuelve_409(
    client: TestClient, auth: dict[str, str]
) -> None:
    archivo = _xml("factura.xml")
    primera = client.post(
        IMPORT, headers=auth, files={"archivo": ("factura.xml", archivo, "application/xml")}
    )
    assert primera.status_code == 201

    segunda = client.post(
        IMPORT, headers=auth, files={"archivo": ("factura.xml", archivo, "application/xml")}
    )
    assert segunda.status_code == 409
    assert segunda.json()["error"]["code"] == "conflict"


def test_importar_como_lector_devuelve_403(
    client: TestClient, auth_lector: dict[str, str]
) -> None:
    respuesta = client.post(
        IMPORT,
        headers=auth_lector,
        files={"archivo": ("factura.xml", _xml("factura.xml"), "application/xml")},
    )
    assert respuesta.status_code == 403
    assert respuesta.json()["error"]["details"]["rol"] == "lector"


def test_importar_ruc_de_otra_empresa_se_rechaza(
    client: TestClient, auth: dict[str, str]
) -> None:
    # El RUC del emisor del XML debe coincidir con el de la empresa de la ruta.
    xml = _xml("factura.xml").replace(b"20123456789", b"20999999999")
    respuesta = client.post(
        IMPORT, headers=auth, files={"archivo": ("factura.xml", xml, "application/xml")}
    )
    assert respuesta.status_code == 422
    assert respuesta.json()["error"]["code"] == "invalid_document"


def test_importar_xml_invalido(client: TestClient, auth: dict[str, str]) -> None:
    respuesta = client.post(
        IMPORT, headers=auth, files={"archivo": ("roto.xml", b"<Invoice", "application/xml")}
    )
    assert respuesta.status_code == 422
    assert respuesta.json()["error"]["code"] == "invalid_document"


def test_importar_archivo_demasiado_grande(client: TestClient, auth: dict[str, str]) -> None:
    """Una subida mayor al máximo se rechaza con 413 y sin tocar la base."""
    enorme = b"<Invoice>" + b"x" * (TAMANO_MAXIMO_XML + 1)

    respuesta = client.post(
        IMPORT, headers=auth, files={"archivo": ("enorme.xml", enorme, "application/xml")}
    )

    assert respuesta.status_code == 413
    error = respuesta.json()["error"]
    assert error["code"] == "file_too_large"
    assert error["details"]["maximo_bytes"] == TAMANO_MAXIMO_XML


class _ArchivoSinTamanoDeclarado:
    """Subida que no declara tamaño (el caso del troceado `Transfer-Encoding`)."""

    size = None

    def __init__(self, datos: bytes) -> None:
        self._buffer = io.BytesIO(datos)

    async def read(self, n: int = -1) -> bytes:
        return self._buffer.read(n)


async def test_la_lectura_aborta_al_pasar_el_limite_sin_leer_todo() -> None:
    maximo = 4096
    archivo = _ArchivoSinTamanoDeclarado(b"x" * (maximo * 100))

    with pytest.raises(FileTooLargeError) as error:
        await leer_con_limite(archivo, maximo)  # type: ignore[arg-type]

    recibido = error.value.details["recibido_bytes"]
    assert recibido > maximo
    # Cortó al primer bloque que pasó del límite: no leyó los 400 KB.
    assert recibido <= maximo + BLOQUE_LECTURA_BYTES


def _sembrar(uow: FakeUnitOfWork, empresa: Company):
    comprobante = construir_comprobante(empresa.id, ruc=RUC_PRINCIPAL, correlativo=7)
    uow.comprobantes.comprobantes[comprobante.id] = comprobante
    return comprobante


def test_patch_n_sap_se_guarda_y_se_limpia(
    client: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    empresa_principal: Company,
) -> None:
    comprobante = _sembrar(uow, empresa_principal)
    url = f"/api/v1/comprobantes/{comprobante.id}"

    guardado = client.patch(url, headers=auth, json={"n_sap": " 4500123 "})
    assert guardado.status_code == 200
    assert guardado.json()["n_sap"] == "4500123"
    assert "check_sap" not in guardado.json()

    # null o vacío lo limpia.
    limpio = client.patch(url, headers=auth, json={"n_sap": ""})
    assert limpio.json()["n_sap"] is None


def test_patch_como_lector_devuelve_403(
    client: TestClient,
    auth_lector: dict[str, str],
    uow: FakeUnitOfWork,
    empresa_principal: Company,
) -> None:
    comprobante = _sembrar(uow, empresa_principal)

    respuesta = client.patch(
        f"/api/v1/comprobantes/{comprobante.id}", headers=auth_lector, json={"n_sap": "1"}
    )

    assert respuesta.status_code == 403
    assert respuesta.json()["error"]["details"]["rol"] == "lector"
    assert uow.comprobantes.comprobantes[comprobante.id].n_sap is None


def test_patch_sin_token_devuelve_401(client: TestClient) -> None:
    respuesta = client.patch(f"/api/v1/comprobantes/{uuid4()}", json={"n_sap": "1"})
    assert respuesta.status_code == 401


def test_patch_comprobante_inexistente_devuelve_404(
    client: TestClient, auth: dict[str, str]
) -> None:
    respuesta = client.patch(f"/api/v1/comprobantes/{uuid4()}", headers=auth, json={"n_sap": "1"})
    assert respuesta.status_code == 404


@pytest.mark.parametrize("cuerpo", [{}, {"check_sap": True}, {"n_sap": "x" * 51}])
def test_patch_cuerpo_invalido_devuelve_422(
    client: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    empresa_principal: Company,
    cuerpo: dict[str, object],
) -> None:
    comprobante = _sembrar(uow, empresa_principal)
    respuesta = client.patch(f"/api/v1/comprobantes/{comprobante.id}", headers=auth, json=cuerpo)
    assert respuesta.status_code == 422
    assert "error" in respuesta.json()


def test_importar_credito_guarda_vencimiento_y_forma_de_pago(
    client: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    empresa_principal: Company,
) -> None:
    respuesta = client.post(
        IMPORT,
        headers=auth,
        files={"archivo": ("f.xml", _xml("factura_credito.xml"), "application/xml")},
    )

    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    assert cuerpo["fecha_vencimiento"] == "2026-11-04"
    assert cuerpo["forma_pago"] == "Crédito 30 días"
    guardado = next(iter(uow.comprobantes.comprobantes.values()))
    assert guardado.fecha_vencimiento is not None
    assert guardado.forma_pago == "Crédito 30 días"


def test_importar_sin_condiciones_de_pago_devuelve_null(
    client: TestClient, auth: dict[str, str], empresa_principal: Company
) -> None:
    respuesta = client.post(
        IMPORT,
        headers=auth,
        files={"archivo": ("f.xml", _xml("factura.xml"), "application/xml")},
    )

    assert respuesta.status_code == 201
    assert respuesta.json()["fecha_vencimiento"] is None
    assert respuesta.json()["forma_pago"] is None
