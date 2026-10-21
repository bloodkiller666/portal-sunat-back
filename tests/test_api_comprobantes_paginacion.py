"""Listado paginado en el servidor: total, orden, filtros nuevos y resumen."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.enums import EstadoArchivo, EstadoComprobante, TipoArchivo
from app.db.models import Archivo, Company
from tests.conftest import RUC_PRINCIPAL
from tests.fakes import FakeUnitOfWork, construir_comprobante

LISTA = f"/api/v1/companies/{RUC_PRINCIPAL}/comprobantes"
RESUMEN = f"{LISTA}/resumen"


def _agregar(uow: FakeUnitOfWork, empresa: Company, **datos):
    comprobante = construir_comprobante(empresa.id, ruc=RUC_PRINCIPAL, **datos)
    uow.comprobantes.comprobantes[uuid4()] = comprobante
    return comprobante


def _con_archivo(uow: FakeUnitOfWork, empresa: Company, comprobante, tipo: TipoArchivo) -> None:
    uow.archivos.archivos.append(
        Archivo(
            id=uuid4(),
            company_id=empresa.id,
            comprobante_id=comprobante.id,
            tipo=tipo,
            nombre=f"{comprobante.numero}.{tipo.value}",
            storage_key=f"k/{uuid4()}",
            estado=EstadoArchivo.subido,
            subido_at=datetime.now(tz=UTC),
        )
    )

def _poblar_100(uow: FakeUnitOfWork, empresa: Company) -> None:
    for i in range(1, 101):
        _agregar(uow, empresa, correlativo=i, fecha_emision=date(2026, 9, 1 + (i % 28)))


def test_pagina_con_total_y_has_more(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    _poblar_100(uow, empresa_principal)

    primera = client.get(LISTA, headers=auth, params={"limit": 25}).json()

    assert len(primera["items"]) == 25
    assert primera["total"] == 100
    assert primera["has_more"] is True

    ultima = client.get(LISTA, headers=auth, params={"limit": 25, "offset": 75}).json()
    assert len(ultima["items"]) == 25
    assert ultima["has_more"] is False


def test_contar_false_omite_el_total(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    _poblar_100(uow, empresa_principal)

    cuerpo = client.get(LISTA, headers=auth, params={"limit": 10, "contar": "false"}).json()

    assert cuerpo["total"] is None
    assert cuerpo["has_more"] is True


def test_total_respeta_los_filtros(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    _poblar_100(uow, empresa_principal)
    _agregar(uow, empresa_principal, correlativo=500, serie="B001", tipo_documento="03")

    cuerpo = client.get(LISTA, headers=auth, params={"tipo_documento": "03", "limit": 10}).json()

    assert cuerpo["total"] == 1


def test_orden_por_importe_con_notas_de_credito_negativas(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    _agregar(uow, empresa_principal, correlativo=1, monto_total=Decimal("500"))
    _agregar(uow, empresa_principal, correlativo=2, monto_total=Decimal("100"))
    _agregar(
        uow, empresa_principal, correlativo=3, tipo_documento="07", serie="FNC1",
        monto_total=Decimal("900"),
    )  # nota de crédito: pesa -900

    asc = client.get(LISTA, headers=auth, params={"orden": "importe", "direccion": "asc"}).json()
    desc = client.get(LISTA, headers=auth, params={"orden": "importe", "direccion": "desc"}).json()

    assert [i["numero"] for i in asc["items"]] == ["00000003", "00000002", "00000001"]
    assert [i["numero"] for i in desc["items"]] == ["00000001", "00000002", "00000003"]


def test_orden_invalido_se_rechaza(client: TestClient, auth: dict[str, str]) -> None:
    # Lista blanca: nunca se arma un `order by` con texto del usuario.
    respuesta = client.get(LISTA, headers=auth, params={"orden": "monto_total; drop table x"})
    assert respuesta.status_code == 422


def test_filtra_por_n_sap(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    con_sap = _agregar(uow, empresa_principal, correlativo=1)
    con_sap.n_sap = "4500926001"
    _agregar(uow, empresa_principal, correlativo=2)

    por_sap = client.get(LISTA, headers=auth, params={"n_sap": "926"}).json()

    assert [i["correlativo"] for i in por_sap["items"]] == [1]


def test_filtra_por_fecha_de_vencimiento(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    for correlativo, vence in ((1, date(2026, 10, 5)), (2, date(2026, 10, 20)), (3, None)):
        c = _agregar(uow, empresa_principal, correlativo=correlativo)
        c.fecha_vencimiento = vence
        c.fecha_emision = date(2026, 10, 16)

    def numeros(**params: str) -> list[int]:
        cuerpo = client.get(LISTA, headers=auth, params=params).json()
        return sorted(i["correlativo"] for i in cuerpo["items"])

    assert numeros(vencimiento_desde="2026-10-10") == [2, 3]
    assert numeros(vencimiento_hasta="2026-10-10") == [1]
    assert numeros(vencimiento_desde="2026-10-01", vencimiento_hasta="2026-10-31") == [1, 2, 3]
    # Sin vencimiento informado (contado) rige la fecha de emisión: 16/10/2026.
    assert numeros(vencimiento_desde="2026-10-16", vencimiento_hasta="2026-10-16") == [3]
    assert 3 not in numeros(vencimiento_desde="2026-10-17")
    assert 3 not in numeros(vencimiento_hasta="2026-10-15")


def test_filtra_por_moneda(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    _agregar(uow, empresa_principal, correlativo=1).moneda = "PEN"
    _agregar(uow, empresa_principal, correlativo=2).moneda = "USD"

    def numeros(**params: str) -> list[int]:
        cuerpo = client.get(LISTA, headers=auth, params=params).json()
        return sorted(i["correlativo"] for i in cuerpo["items"])

    assert numeros(moneda="PEN") == [1]
    assert numeros(moneda="USD") == [2]
    assert numeros() == [1, 2]


def test_rechaza_un_rango_de_vencimiento_invertido(
    client: TestClient, auth: dict[str, str]
) -> None:
    respuesta = client.get(
        LISTA,
        headers=auth,
        params={"vencimiento_desde": "2026-10-10", "vencimiento_hasta": "2026-10-01"},
    )
    assert respuesta.status_code == 422


def test_busqueda_por_documento_cliente_y_ruc(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    a = _agregar(uow, empresa_principal, correlativo=15979, serie="FF01")
    a.razon_social_receptor = "CONSTRUCTORA ANDES SAC"
    a.ruc_receptor = "20555666777"
    _agregar(uow, empresa_principal, correlativo=2, serie="FF01")

    por_numero = client.get(LISTA, headers=auth, params={"q": "FF01-00015979"}).json()
    por_cliente = client.get(LISTA, headers=auth, params={"q": "andes"}).json()
    por_ruc = client.get(LISTA, headers=auth, params={"q": "2055566"}).json()

    for cuerpo in (por_numero, por_cliente, por_ruc):
        assert [i["correlativo"] for i in cuerpo["items"]] == [15979]


def test_filtra_por_respaldo(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    completo = _agregar(uow, empresa_principal, correlativo=1)
    solo_xml = _agregar(uow, empresa_principal, correlativo=2)
    _agregar(uow, empresa_principal, correlativo=3)
    _con_archivo(uow, empresa_principal, completo, TipoArchivo.xml)
    _con_archivo(uow, empresa_principal, completo, TipoArchivo.cdr)
    _con_archivo(uow, empresa_principal, solo_xml, TipoArchivo.xml)

    def numeros(respaldo: str) -> list[int]:
        cuerpo = client.get(
            LISTA, headers=auth, params={"respaldo": respaldo, "orden": "numero",
                                         "direccion": "asc"},
        ).json()
        return [i["correlativo"] for i in cuerpo["items"]]

    assert numeros("completo") == [1]
    assert numeros("sin_cdr") == [2, 3]
    assert numeros("sin_xml") == [3]


def test_resumen_calcula_estados_neto_y_cobertura(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    factura = _agregar(
        uow, empresa_principal, correlativo=1, estado=EstadoComprobante.aprobado,
        monto_total=Decimal("1000"),
    )
    _agregar(
        uow, empresa_principal, correlativo=2, tipo_documento="07", serie="FNC1",
        estado=EstadoComprobante.aprobado, monto_total=Decimal("200"),
    )
    _agregar(  # anulado y rechazado no suman al facturado
        uow, empresa_principal, correlativo=3, estado=EstadoComprobante.anulado,
        monto_total=Decimal("999"),
    )
    _agregar(
        uow, empresa_principal, correlativo=4, estado=EstadoComprobante.rechazado,
        monto_total=Decimal("999"),
    )
    _con_archivo(uow, empresa_principal, factura, TipoArchivo.xml)

    cuerpo = client.get(RESUMEN, headers=auth).json()

    assert cuerpo["total"] == 4
    assert cuerpo["por_estado"] == {
        "pendiente": 0, "aprobado": 2, "rechazado": 1, "anulado": 1,
    }
    assert Decimal(cuerpo["facturado_neto"]["PEN"]) == Decimal("800")  # 1000 - 200
    # Cobertura sobre lo exigible: todo menos el anulado.
    assert cuerpo["cobertura"] == {"exigibles": 3, "con_xml": 1, "con_cdr": 0, "con_pdf": 0}


def test_resumen_respeta_el_rango_de_fechas(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    _agregar(uow, empresa_principal, correlativo=1, fecha_emision=date(2026, 9, 10))
    _agregar(uow, empresa_principal, correlativo=2, fecha_emision=date(2026, 10, 10))

    cuerpo = client.get(
        RESUMEN, headers=auth, params={"fecha_desde": "2026-10-01", "fecha_hasta": "2026-10-31"}
    ).json()

    assert cuerpo["total"] == 1


def test_resumen_de_empresa_ajena_se_deniega(client: TestClient, auth: dict[str, str]) -> None:
    respuesta = client.get("/api/v1/companies/20111111111/comprobantes/resumen", headers=auth)
    assert respuesta.status_code in (403, 404)
