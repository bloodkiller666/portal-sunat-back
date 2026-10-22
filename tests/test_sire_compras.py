"""Compras desde SIRE (RCE): parser, cliente HTTP y servicio/API. SUNAT siempre mockeado."""

from __future__ import annotations

import io
import zipfile
from datetime import date
from decimal import Decimal

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.enums import EstadoComprobante, OrigenComprobante, SunatAmbiente
from app.core.errors import SunatFaultError
from app.db.models import Company
from app.sunat.endpoints import (
    SIRE_ARCHIVO_REPORTE_URL,
    SIRE_EXPORTAR_PROPUESTA_RCE_URL,
    SIRE_TICKETS_URL,
    SIRE_TOKEN_URL,
)
from app.sunat.sire import LIBRO_COMPRAS, SireClient, SirePeriodoSinDatosError
from app.sunat.sire_rce import parse_propuesta_rce
from app.sunat.sire_rvie import monto_en_moneda
from tests.conftest import RUC_PRINCIPAL
from tests.fakes import FakeSireClient, FakeUnitOfWork
from tests.test_sire_client import (
    CLIENT_ID,
    TOKEN_URL,
    _credenciales,
    _registro_ticket,
    _token_ok,
)

SYNC = f"/api/v1/companies/{RUC_PRINCIPAL}/sire/compras/sync"
SYNC_VENTAS = f"/api/v1/companies/{RUC_PRINCIPAL}/sire/ventas/sync"

# Cabecera real de la propuesta RCE (descarga de producción, 202609): trae tildes y un doble
# espacio en «Razón  Social». Se omiten las 39 columnas CLU de uso interno de SUNAT.
CABECERA_RCE = [
    "RUC",
    "Apellidos y Nombres o Razón social",
    "Periodo",
    "CAR SUNAT",
    "Fecha de emisión",
    "Fecha Vcto/Pago",
    "Tipo CP/Doc.",
    "Serie del CDP",
    "Año",
    "Nro CP o Doc. Nro Inicial (Rango)",
    "Nro Final (Rango)",
    "Tipo Doc Identidad",
    "Nro Doc Identidad",
    "Apellidos Nombres/ Razón  Social",
    "BI Gravado DG",
    "IGV / IPM DG",
    "BI Gravado DGNG",
    "IGV / IPM DGNG",
    "BI Gravado DNG",
    "IGV / IPM DNG",
    "Valor Adq. NG",
    "ISC",
    "ICBPER",
    "Otros Trib/ Cargos",
    "Total CP",
    "Moneda",
    "Tipo de Cambio",
    "Fecha Emisión Doc Modificado",
    "Tipo CP Modificado",
    "Serie CP Modificado",
    "COD. DAM O DSI",
    "Nro CP Modificado",
    "Clasif de Bss y Sss",
    "ID Proyecto Operadores",
    "PorcPart",
    "IMB",
    "CAR Orig/ Ind E o I",
    "Detracción",
    "Tipo de Nota",
    "Est. Comp.",
    "Incal",
]


def _fila(**campos: str) -> str:
    base = {
        "RUC": RUC_PRINCIPAL,
        "Apellidos y Nombres o Razón social": "TDM ASFALTOS S.A.C.",
        "Periodo": "202609",
        "Fecha de emisión": "09/09/2026",
        "Tipo CP/Doc.": "01",
        "Serie del CDP": "E001",
        "Año": "2026",
        "Nro CP o Doc. Nro Inicial (Rango)": "10",
        "Tipo Doc Identidad": "6",
        "Nro Doc Identidad": "20613171631",
        "Apellidos Nombres/ Razón  Social": "PROVEEDOR UNO SAC",
        "BI Gravado DG": "400.00",
        "IGV / IPM DG": "72.00",
        "Total CP": "472.00",
        "Moneda": "PEN",
        "Tipo de Cambio": "1.000",
        "Est. Comp.": "1",
        "Incal": "0",
    }
    base.update(campos)
    return "|".join(base.get(columna, "") for columna in CABECERA_RCE)


def _zip(*filas: str, cabecera: list[str] | None = None) -> bytes:
    texto = "\r\n".join(["|".join(cabecera or CABECERA_RCE), *filas]) + "\r\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        # Sin BOM, como lo entrega SUNAT.
        zf.writestr("20514831620-20261021-1248-propuesta.txt", texto.encode("utf-8"))
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
def test_el_proveedor_es_el_emisor_y_la_empresa_el_adquirente() -> None:
    resultado = parse_propuesta_rce(
        _zip(_fila(**{"Fecha Vcto/Pago": "09/10/2026"})), ruc_esperado=RUC_PRINCIPAL
    )

    (compra,) = resultado.registros
    assert compra.ruc_proveedor == "20613171631"
    assert compra.razon_social_proveedor == "PROVEEDOR UNO SAC"
    assert compra.ruc_adquirente == RUC_PRINCIPAL
    assert (compra.tipo_documento, compra.serie, compra.correlativo) == ("01", "E001", 10)
    assert compra.fecha_emision == date(2026, 9, 9)
    assert compra.fecha_vencimiento == date(2026, 10, 9)
    assert compra.monto_total == Decimal("472.00")
    assert not compra.anulado


def test_una_nota_de_credito_negativa_se_guarda_en_positivo() -> None:
    zip_ = _zip(_fila(**{"Tipo CP/Doc.": "07", "Total CP": "-2960.41"}))

    (nota,) = parse_propuesta_rce(zip_, ruc_esperado=RUC_PRINCIPAL).registros

    assert nota.monto_total == Decimal("2960.41")


def test_un_comprobante_en_dolares_se_convierte_de_soles_a_su_moneda() -> None:
    # SIRE informa el total en soles: 68 411,09 / 3,402 = 20 109,08 USD (comprobado en Bizlinks).
    zip_ = _zip(_fila(Moneda="USD", **{"Total CP": "68411.09", "Tipo de Cambio": "3.402"}))

    (compra,) = parse_propuesta_rce(zip_, ruc_esperado=RUC_PRINCIPAL).registros

    assert compra.moneda == "USD"
    assert compra.monto_total == Decimal("20109.08")
    assert compra.monto_pen == Decimal("68411.09")
    assert compra.tipo_cambio == Decimal("3.402")


def test_sin_tipo_de_cambio_valido_no_se_inventa_la_conversion() -> None:
    assert monto_en_moneda(Decimal("100"), "USD", None) == (Decimal("100"), None)
    assert monto_en_moneda(Decimal("100"), "USD", Decimal("0")) == (Decimal("100"), None)
    assert monto_en_moneda(Decimal("100"), "PEN", Decimal("1")) == (Decimal("100"), Decimal("100"))


def test_se_omite_lo_que_no_es_una_compra_con_proveedor_identificado() -> None:
    zip_ = _zip(
        _fila(),
        # Declaración aduanera: tipo 50, proveedor sin RUC.
        _fila(**{"Tipo CP/Doc.": "50", "Tipo Doc Identidad": "0", "Nro Doc Identidad": "74337240"}),
        # Tipo soportado pero el proveedor no tiene RUC (no cabría en `ruc_emisor`).
        _fila(**{"Nro CP o Doc. Nro Inicial (Rango)": "11", "Tipo Doc Identidad": "1"}),
        _fila(**{"Nro CP o Doc. Nro Inicial (Rango)": "0"}),
    )

    resultado = parse_propuesta_rce(zip_, ruc_esperado=RUC_PRINCIPAL)

    assert len(resultado.registros) == 1
    assert resultado.omitidos == {
        "tipo_no_soportado": 1,
        "proveedor_sin_ruc": 1,
        "numeracion_invalida": 1,
    }


def test_un_ruc_distinto_al_de_la_empresa_se_rechaza() -> None:
    with pytest.raises(SunatFaultError):
        parse_propuesta_rce(_zip(_fila(RUC="20999999999")), ruc_esperado=RUC_PRINCIPAL)


def test_si_cambia_el_formato_se_avisa_con_las_columnas_que_faltan() -> None:
    cabecera = [c for c in CABECERA_RCE if c != "Total CP"]

    with pytest.raises(SunatFaultError) as exc:
        parse_propuesta_rce(_zip(cabecera=cabecera), ruc_esperado=RUC_PRINCIPAL)

    assert exc.value.details == {"faltantes": ["total cp"]}


# --------------------------------------------------------------------------- #
# Cliente HTTP (URLs del manual SIRE Compras v28)
# --------------------------------------------------------------------------- #
@pytest.fixture
def cliente(settings: Settings) -> SireClient:
    return SireClient(_credenciales(), settings)


@respx.mock
def test_la_propuesta_de_compras_usa_el_servicio_rce_con_origen_api(cliente: SireClient) -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_ok())
    ruta = respx.get(SIRE_EXPORTAR_PROPUESTA_RCE_URL.format(periodo="202609")).mock(
        return_value=httpx.Response(200, json={"numTicket": "20260300000097"})
    )

    assert cliente.solicitar_propuesta("202609", LIBRO_COMPRAS) == "20260300000097"

    assert "exportacioncomprobantepropuesta" in str(ruta.calls.last.request.url)
    parametros = ruta.calls.last.request.url.params
    assert parametros["codTipoArchivo"] == "0"
    assert parametros["codOrigenEnvio"] == "2"


@respx.mock
def test_ticket_y_descarga_de_compras_piden_el_libro_080000(cliente: SireClient) -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_ok())
    tickets = respx.get(SIRE_TICKETS_URL).mock(
        return_value=httpx.Response(200, json={"registros": [_registro_ticket()]})
    )
    archivo = respx.get(SIRE_ARCHIVO_REPORTE_URL).mock(
        return_value=httpx.Response(200, content=b"PK-zip")
    )

    estado = cliente.consultar_ticket("20260300000071", "202609", LIBRO_COMPRAS)
    assert cliente.descargar_archivo(estado, estado.archivos[0], LIBRO_COMPRAS) == b"PK-zip"

    assert tickets.calls.last.request.url.params["codLibro"] == "080000"
    assert archivo.calls.last.request.url.params["codLibro"] == "080000"


@respx.mock
def test_periodo_de_compras_sin_documentos_no_es_un_fallo(cliente: SireClient) -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_ok())
    respx.get(SIRE_EXPORTAR_PROPUESTA_RCE_URL.format(periodo="202609")).mock(
        return_value=httpx.Response(
            422, json={"cod": "422", "errors": [{"cod": "1518", "msg": "No existen documentos"}]}
        )
    )

    with pytest.raises(SirePeriodoSinDatosError):
        cliente.solicitar_propuesta("202609", LIBRO_COMPRAS)


def test_el_token_se_pide_con_el_client_id_de_la_empresa() -> None:
    assert CLIENT_ID in SIRE_TOKEN_URL.format(client_id=CLIENT_ID)


# --------------------------------------------------------------------------- #
# Servicio y endpoint
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _sin_esperas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.sire.INTERVALO_SEGUNDOS", 0)


def _sincronizar(client: TestClient, headers: dict[str, str], **cuerpo: object):
    return client.post(SYNC, headers=headers, json={"periodo": "202609", **cuerpo})


def test_sincronizar_registra_las_compras_con_el_proveedor_como_emisor(
    client: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    sire: FakeSireClient,
    empresa_principal: Company,
) -> None:
    sire.zips = [
        _zip(
            _fila(),
            _fila(**{"Nro CP o Doc. Nro Inicial (Rango)": "11", "Est. Comp.": "2"}),
        )
    ]

    respuesta = _sincronizar(client, auth)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert (cuerpo["estado"], cuerpo["nuevos"], cuerpo["anulados"]) == ("completado", 2, 1)
    assert sire.libros == ["rce"]
    guardados = sorted(uow.comprobantes.comprobantes.values(), key=lambda c: c.correlativo)
    primero = guardados[0]
    assert primero.origen is OrigenComprobante.recibido
    assert primero.company_id == empresa_principal.id
    assert primero.ruc_emisor == "20613171631"
    assert primero.razon_social_emisor == "PROVEEDOR UNO SAC"
    assert primero.ruc_receptor == RUC_PRINCIPAL
    assert primero.emisor_tipo_doc == "6"
    assert primero.fuente == "sire"
    assert primero.sunat_ambiente is SunatAmbiente.prod
    assert primero.nombre_archivo == "20613171631-01-E001-00000010"
    assert [c.estado for c in guardados] == [EstadoComprobante.aprobado, EstadoComprobante.anulado]


def test_dos_proveedores_pueden_tener_la_misma_serie_y_numero(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, sire: FakeSireClient
) -> None:
    sire.zips = [
        _zip(_fila(), _fila(**{"Nro Doc Identidad": "20100128218"})),
    ]

    cuerpo = _sincronizar(client, auth).json()

    assert (cuerpo["nuevos"], cuerpo["ya_existian"]) == (2, 0)
    assert {c.ruc_emisor for c in uow.comprobantes.comprobantes.values()} == {
        "20613171631",
        "20100128218",
    }


def test_sincronizar_compras_dos_veces_no_duplica(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, sire: FakeSireClient
) -> None:
    sire.zips = [_zip(_fila())]

    primera = _sincronizar(client, auth).json()
    segunda = _sincronizar(client, auth, forzar=True).json()

    assert (primera["nuevos"], segunda["nuevos"], segunda["ya_existian"]) == (1, 0, 1)
    assert len(uow.comprobantes.comprobantes) == 1


def test_un_periodo_de_compras_ya_cargado_se_rechaza(
    client: TestClient, auth: dict[str, str], sire: FakeSireClient
) -> None:
    sire.zips = [_zip(_fila())]
    assert _sincronizar(client, auth).status_code == 200
    sire.solicitudes.clear()

    r = _sincronizar(client, auth)

    assert r.status_code == 409
    assert r.json()["error"]["code"] == "periodo_ya_cargado"
    assert sire.solicitudes == []


def test_ventas_y_compras_del_mismo_mes_no_se_bloquean_entre_si(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, sire: FakeSireClient
) -> None:
    from tests.fakes import construir_zip_propuesta, fila_propuesta

    sire.zips = [construir_zip_propuesta([fila_propuesta(RUC_PRINCIPAL)])]
    assert client.post(SYNC_VENTAS, headers=auth, json={"periodo": "202609"}).status_code == 200
    sire.zips = [_zip(_fila())]

    r = _sincronizar(client, auth)

    assert r.status_code == 200
    assert {c.origen for c in uow.comprobantes.comprobantes.values()} == {
        OrigenComprobante.emitido,
        OrigenComprobante.recibido,
    }


def test_las_compras_se_listan_aparte_de_las_ventas(
    client: TestClient, auth: dict[str, str], sire: FakeSireClient
) -> None:
    sire.zips = [_zip(_fila())]
    _sincronizar(client, auth)
    lista = f"/api/v1/companies/{RUC_PRINCIPAL}/comprobantes"

    compras = client.get(lista, headers=auth, params={"origen": "recibido"}).json()
    ventas = client.get(lista, headers=auth, params={"origen": "emitido"}).json()

    assert [c["ruc_emisor"] for c in compras["items"]] == ["20613171631"]
    assert ventas["items"] == []


def test_un_lector_no_puede_sincronizar_compras(
    client: TestClient, auth_lector: dict[str, str]
) -> None:
    assert _sincronizar(client, auth_lector).status_code == 403


def test_en_compras_un_tipo_de_cambio_cero_tampoco_se_guarda() -> None:
    zip_ = _zip(_fila(Moneda="USD", **{"Total CP": "100.00", "Tipo de Cambio": "0.000"}))

    (compra,) = parse_propuesta_rce(zip_, ruc_esperado=RUC_PRINCIPAL).registros

    assert compra.tipo_cambio is None and compra.monto_pen is None
    assert compra.monto_total == Decimal("100.00")  # sin conversión posible: se conserva el total
