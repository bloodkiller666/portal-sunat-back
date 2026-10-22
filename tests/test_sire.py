"""Pruebas de la sincronización de ventas desde SIRE: parser, servicio y API."""

from __future__ import annotations

import io
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.enums import EstadoComprobante, SunatAmbiente
from app.core.errors import SunatFaultError
from app.db.models import Company
from app.sunat import sire_rvie
from app.sunat.sire import SirePeriodoSinDatosError
from app.sunat.sire_rvie import CABECERA, parse_propuesta_zip
from tests.conftest import RUC_PRINCIPAL
from tests.fakes import (
    FakeSireClient,
    FakeUnitOfWork,
    construir_comprobante,
    construir_zip_propuesta,
    fila_propuesta,
)

SYNC = f"/api/v1/companies/{RUC_PRINCIPAL}/sire/ventas/sync"
UBL = Path(__file__).parent / "fixtures" / "ubl"
NRO = "Nro CP o Doc. Nro Inicial (Rango)"


def _fila(**campos: str) -> dict[str, str]:
    return fila_propuesta(RUC_PRINCIPAL, **campos)


# --------------------------------------------------------------------------- #
# Parser de la propuesta
# --------------------------------------------------------------------------- #
def test_factura_boleta_y_notas_se_leen_con_sus_datos() -> None:
    zip_ = construir_zip_propuesta(
        [
            _fila(**{"Fecha Vcto/Pago": "15/10/2026"}),
            _fila(
                **{
                    "Tipo CP/Doc.": "03",
                    "Serie del CDP": "BB01",
                    "Tipo Doc Identidad": "1",
                    "Nro Doc Identidad": "12345678",
                }
            ),
            _fila(**{"Tipo CP/Doc.": "07", "Serie del CDP": "FNC1", "Total CP": "-59.00"}),
        ]
    )

    resultado = parse_propuesta_zip(zip_, ruc_esperado=RUC_PRINCIPAL)

    factura, boleta, nota = resultado.registros
    assert (factura.tipo_documento, factura.serie, factura.correlativo) == ("01", "FF01", 1)
    assert factura.fecha_emision == date(2026, 9, 1)
    assert factura.fecha_vencimiento == date(2026, 10, 15)
    assert factura.monto_total == Decimal("118.00")
    assert factura.ruc_receptor == "20987654321"
    assert factura.razon_social_receptor == "CLIENTE EJEMPLO SAC"
    # Boleta a DNI: el documento no es un RUC.
    assert boleta.ruc_receptor is None
    assert boleta.receptor_doc == "12345678"
    # La nota de crédito llega negativa y se guarda en positivo (como con el XML).
    assert nota.monto_total == Decimal("59.00")
    assert not resultado.omitidos


def test_comprobante_dado_de_baja_es_anulado() -> None:
    zip_ = construir_zip_propuesta(
        [_fila(**{"Est. Comp": "2", "Total CP": "0"}), _fila(**{NRO: "2"})]
    )

    anulado, vigente = parse_propuesta_zip(zip_, ruc_esperado=RUC_PRINCIPAL).registros

    assert anulado.anulado is True
    assert anulado.monto_total == Decimal("0")
    assert vigente.anulado is False


def test_filas_no_representables_se_omiten_y_se_cuentan() -> None:
    zip_ = construir_zip_propuesta(
        [
            _fila(),
            _fila(**{"Tipo CP/Doc.": "00", "Serie del CDP": "X001"}),
            _fila(**{"Tipo CP/Doc.": "03", "Serie del CDP": "BB01", "Nro Final (Rango)": "50"}),
            _fila(**{NRO: "abc"}),
            _fila(**{"Fecha de emisión": "2026-09-01"}),
            _fila(**{"Total CP": "no-numero"}),
        ]
    )

    resultado = parse_propuesta_zip(zip_, ruc_esperado=RUC_PRINCIPAL)

    assert len(resultado.registros) == 1
    assert resultado.omitidos == {
        "tipo_no_soportado": 1,
        "boletas_en_rango": 1,
        "numeracion_invalida": 1,
        "fecha_invalida": 1,
        "importe_invalido": 1,
    }


def test_una_propuesta_de_otro_ruc_se_rechaza() -> None:
    zip_ = construir_zip_propuesta([fila_propuesta("20999999999")])

    with pytest.raises(SunatFaultError) as error:
        parse_propuesta_zip(zip_, ruc_esperado=RUC_PRINCIPAL)

    assert "otro RUC" in error.value.message


def test_si_cambia_el_formato_de_sunat_se_avisa() -> None:
    cabecera = tuple(c for c in CABECERA if c != "Total CP")
    zip_ = construir_zip_propuesta([_fila()], cabecera=cabecera)

    with pytest.raises(SunatFaultError) as error:
        parse_propuesta_zip(zip_, ruc_esperado=RUC_PRINCIPAL)

    assert error.value.details["faltantes"] == ["Total CP"]


def test_zip_invalido_se_rechaza() -> None:
    with pytest.raises(SunatFaultError):
        parse_propuesta_zip(b"esto no es un zip", ruc_esperado=RUC_PRINCIPAL)


def test_zip_con_demasiados_archivos_se_rechaza() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for i in range(sire_rvie.MAX_ENTRADAS_ZIP + 1):
            zf.writestr(f"{i}.txt", "x")

    with pytest.raises(SunatFaultError):
        parse_propuesta_zip(buffer.getvalue(), ruc_esperado=RUC_PRINCIPAL)


def test_el_tope_de_filas_protege_la_memoria(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sire_rvie, "MAX_FILAS", 2)
    zip_ = construir_zip_propuesta([_fila(**{NRO: str(n)}) for n in range(1, 5)])

    with pytest.raises(SunatFaultError):
        parse_propuesta_zip(zip_, ruc_esperado=RUC_PRINCIPAL)


def test_el_tope_de_tamano_aplica_aunque_el_zip_mienta(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sire_rvie, "MAX_BYTES_TXT", 50)
    zip_ = construir_zip_propuesta([_fila()])

    with pytest.raises(SunatFaultError):
        parse_propuesta_zip(zip_, ruc_esperado=RUC_PRINCIPAL)


def test_fila_incompleta_se_omite() -> None:
    texto = "|".join(CABECERA) + "\r\n" + "20123456789|solo|dos\r\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("p.txt", texto.encode("utf-8-sig"))

    resultado = parse_propuesta_zip(buffer.getvalue(), ruc_esperado=RUC_PRINCIPAL)

    assert resultado.omitidos == {"fila_incompleta": 1}


# --------------------------------------------------------------------------- #
# Servicio y endpoint
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _sin_esperas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.sire.INTERVALO_SEGUNDOS", 0)


def _sincronizar(client: TestClient, headers: dict[str, str], **cuerpo: str):
    return client.post(SYNC, headers=headers, json={"periodo": "202609", **cuerpo})


def test_sincronizar_registra_las_ventas_como_comprobantes_reales(
    client: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    sire: FakeSireClient,
    empresa_principal: Company,
) -> None:
    sire.zips = [
        construir_zip_propuesta(
            [
                _fila(),
                _fila(**{NRO: "2", "Total CP": "236.00"}),
                _fila(**{NRO: "3", "Est. Comp": "2", "Total CP": "0"}),
            ]
        )
    ]

    respuesta = _sincronizar(client, auth)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "completado"
    assert (cuerpo["nuevos"], cuerpo["ya_existian"], cuerpo["anulados"]) == (3, 0, 1)
    assert cuerpo["ticket"] == sire.ticket
    guardados = sorted(uow.comprobantes.comprobantes.values(), key=lambda c: c.correlativo)
    assert [c.estado for c in guardados] == [
        EstadoComprobante.aprobado,
        EstadoComprobante.aprobado,
        EstadoComprobante.anulado,
    ]
    primero = guardados[0]
    assert primero.company_id == empresa_principal.id
    assert primero.fuente == "sire"
    # SIRE solo existe en producción, aunque el portal corra en beta.
    assert primero.sunat_ambiente is SunatAmbiente.prod
    assert primero.nombre_archivo == f"{RUC_PRINCIPAL}-01-FF01-00000001"
    assert primero.numero == "00000001"


def test_los_comprobantes_sincronizados_aparecen_en_el_listado(
    client: TestClient, auth: dict[str, str], sire: FakeSireClient
) -> None:
    sire.zips = [construir_zip_propuesta([_fila()])]
    _sincronizar(client, auth)

    lista = client.get(f"/api/v1/companies/{RUC_PRINCIPAL}/comprobantes", headers=auth).json()

    assert [c["nombre_archivo"] for c in lista["items"]] == [f"{RUC_PRINCIPAL}-01-FF01-00000001"]
    assert lista["items"][0]["fuente"] == "sire"
    assert lista["items"][0]["estado"] == "aprobado"


def test_sincronizar_dos_veces_no_duplica(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, sire: FakeSireClient
) -> None:
    sire.zips = [construir_zip_propuesta([_fila(), _fila(**{NRO: "2"})])]

    primera = _sincronizar(client, auth).json()
    segunda = _sincronizar(client, auth, forzar=True).json()

    assert (primera["nuevos"], primera["ya_existian"]) == (2, 0)
    assert (segunda["nuevos"], segunda["ya_existian"]) == (0, 2)
    assert len(uow.comprobantes.comprobantes) == 2


def test_un_periodo_ya_cargado_se_rechaza_sin_llamar_a_sunat(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, sire: FakeSireClient
) -> None:
    sire.zips = [construir_zip_propuesta([_fila(), _fila(**{NRO: "2"})])]
    assert _sincronizar(client, auth).status_code == 200
    sire.solicitudes.clear()

    r = _sincronizar(client, auth)

    assert r.status_code == 409
    error = r.json()["error"]
    assert error["code"] == "periodo_ya_cargado"
    assert error["details"] == {"periodo": "202609", "cargados": 2}
    assert sire.solicitudes == []
    assert len(uow.comprobantes.comprobantes) == 2


def test_otro_periodo_no_se_ve_afectado_por_uno_cargado(
    client: TestClient, auth: dict[str, str], sire: FakeSireClient
) -> None:
    sire.zips = [construir_zip_propuesta([_fila()])]
    assert _sincronizar(client, auth).status_code == 200

    assert _sincronizar(client, auth, periodo="202608").status_code == 200


def test_lo_ya_registrado_no_se_pisa(
    client: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    sire: FakeSireClient,
    empresa_principal: Company,
) -> None:
    previo = construir_comprobante(
        empresa_principal.id,
        ruc=RUC_PRINCIPAL,
        serie="FF01",
        correlativo=1,
        ambiente=SunatAmbiente.prod,
        estado=EstadoComprobante.rechazado,
    )
    uow.comprobantes.comprobantes[previo.id] = previo
    sire.zips = [construir_zip_propuesta([_fila()])]

    cuerpo = _sincronizar(client, auth).json()

    assert (cuerpo["nuevos"], cuerpo["ya_existian"]) == (0, 1)
    assert uow.comprobantes.comprobantes[previo.id].estado is EstadoComprobante.rechazado


def test_los_omitidos_se_informan(
    client: TestClient, auth: dict[str, str], sire: FakeSireClient
) -> None:
    sire.zips = [
        construir_zip_propuesta([_fila(), _fila(**{"Tipo CP/Doc.": "00", "Serie del CDP": "X001"})])
    ]

    cuerpo = _sincronizar(client, auth).json()

    assert cuerpo["nuevos"] == 1
    assert cuerpo["omitidos"] == {"tipo_no_soportado": 1}
    assert cuerpo["total_filas"] == 2


def test_un_zip_particionado_se_une(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, sire: FakeSireClient
) -> None:
    sire.zips = [
        construir_zip_propuesta([_fila()]),
        construir_zip_propuesta([_fila(**{NRO: "2"})]),
    ]

    cuerpo = _sincronizar(client, auth).json()

    assert cuerpo["nuevos"] == 2
    assert sire.descargas == 2


def test_espera_a_que_el_ticket_termine(
    client: TestClient, auth: dict[str, str], sire: FakeSireClient
) -> None:
    sire.estados = ["01", "05", "06"]
    sire.zips = [construir_zip_propuesta([_fila()])]

    cuerpo = _sincronizar(client, auth).json()

    assert cuerpo["estado"] == "completado"
    assert sire.consultas == 3
    assert sire.solicitudes == ["202609"]


def test_si_sunat_tarda_devuelve_el_ticket_y_se_retoma_sin_pedir_otro(
    client: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    sire: FakeSireClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.services.sire.ESPERA_MAXIMA_SEGUNDOS", 0)
    sire.estados = ["05", "06"]
    sire.zips = [construir_zip_propuesta([_fila()])]

    pendiente = _sincronizar(client, auth).json()

    assert pendiente["estado"] == "en_proceso"
    assert pendiente["ticket"] == sire.ticket
    assert not uow.comprobantes.comprobantes

    retomada = _sincronizar(client, auth, ticket=pendiente["ticket"]).json()

    assert retomada["estado"] == "completado"
    assert retomada["nuevos"] == 1
    # La segunda llamada reutiliza el ticket: no se pidió otra propuesta.
    assert sire.solicitudes == ["202609"]


def test_periodo_sin_comprobantes(
    client: TestClient, auth: dict[str, str], sire: FakeSireClient
) -> None:
    sire.error_solicitud = SirePeriodoSinDatosError()

    respuesta = _sincronizar(client, auth)

    assert respuesta.status_code == 200
    assert respuesta.json()["estado"] == "sin_datos"
    assert respuesta.json()["nuevos"] == 0


def test_ticket_terminado_sin_archivos_es_sin_datos(
    client: TestClient, auth: dict[str, str], sire: FakeSireClient
) -> None:
    sire.zips = []

    assert _sincronizar(client, auth).json()["estado"] == "sin_datos"


def test_ticket_con_errores_devuelve_502(
    client: TestClient, auth: dict[str, str], sire: FakeSireClient
) -> None:
    sire.estados = ["03"]

    respuesta = _sincronizar(client, auth)

    assert respuesta.status_code == 502
    assert respuesta.json()["error"]["code"] == "sunat_fault"


def test_estado_de_ticket_desconocido_devuelve_502(
    client: TestClient, auth: dict[str, str], sire: FakeSireClient
) -> None:
    sire.estados = ["99"]

    assert _sincronizar(client, auth).status_code == 502


def test_un_zip_de_otro_ruc_no_registra_nada(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, sire: FakeSireClient
) -> None:
    sire.zips = [construir_zip_propuesta([fila_propuesta("20999999999")])]

    respuesta = _sincronizar(client, auth)

    assert respuesta.status_code == 502
    assert not uow.comprobantes.comprobantes


@pytest.mark.parametrize("periodo", ["2026-09", "202613", "202600", "abcdef", "20269", ""])
def test_periodo_con_formato_invalido_devuelve_422(
    client: TestClient, auth: dict[str, str], periodo: str
) -> None:
    respuesta = client.post(SYNC, headers=auth, json={"periodo": periodo})

    assert respuesta.status_code == 422
    assert respuesta.json()["error"]["code"] == "periodo_invalido"


def test_periodo_futuro_devuelve_422(client: TestClient, auth: dict[str, str]) -> None:
    respuesta = client.post(SYNC, headers=auth, json={"periodo": "209912"})

    assert respuesta.status_code == 422


def test_ticket_con_formato_invalido_devuelve_422(client: TestClient, auth: dict[str, str]) -> None:
    respuesta = _sincronizar(client, auth, ticket="1; drop table")

    assert respuesta.status_code == 422


def test_lector_no_puede_sincronizar(
    client: TestClient, auth_lector: dict[str, str], uow: FakeUnitOfWork
) -> None:
    respuesta = _sincronizar(client, auth_lector)

    assert respuesta.status_code == 403
    assert not uow.comprobantes.comprobantes


def test_sin_token_devuelve_401(client: TestClient) -> None:
    assert client.post(SYNC, json={"periodo": "202609"}).status_code == 401


def test_empresa_ajena_devuelve_403(client: TestClient, auth: dict[str, str]) -> None:
    respuesta = client.post(
        "/api/v1/companies/20999999999/sire/ventas/sync",
        headers=auth,
        json={"periodo": "202609"},
    )

    assert respuesta.status_code in (403, 404)


# --------------------------------------------------------------------------- #
# El XML completa lo que vino de SIRE
# --------------------------------------------------------------------------- #
def test_importar_el_xml_completa_el_registro_de_sire(
    client: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    empresa_principal: Company,
) -> None:
    desde_sire = construir_comprobante(
        empresa_principal.id, ruc=RUC_PRINCIPAL, serie="F001", correlativo=1
    ).model_copy(update={"fuente": "sire", "monto_total": Decimal("1.00")})
    uow.comprobantes.comprobantes[desde_sire.id] = desde_sire

    respuesta = client.post(
        f"/api/v1/companies/{RUC_PRINCIPAL}/comprobantes/import",
        headers=auth,
        files={"archivo": ("factura.xml", (UBL / "factura_con_items.xml").read_bytes())},
    )

    assert respuesta.status_code == 201
    assert respuesta.json()["id"] == str(desde_sire.id)
    assert respuesta.json()["fuente"] == "xml"
    assert len(uow.comprobantes.comprobantes) == 1
    assert uow.comprobantes.comprobantes[desde_sire.id].monto_total == Decimal("188.80")
    detalle = client.get(f"/api/v1/comprobantes/{desde_sire.id}", headers=auth).json()
    assert len(detalle["items"]) > 0


def test_un_xml_repetido_sigue_dando_409(
    client: TestClient, auth: dict[str, str], empresa_principal: Company
) -> None:
    archivo = {"archivo": ("factura.xml", (UBL / "factura.xml").read_bytes())}
    url = f"/api/v1/companies/{RUC_PRINCIPAL}/comprobantes/import"

    assert client.post(url, headers=auth, files=archivo).status_code == 201
    assert client.post(url, headers=auth, files=archivo).status_code == 409


# --------------------------------------------------------------------------- #
# Evidencia: el ZIP original de SUNAT se archiva en R2 con su hash
# --------------------------------------------------------------------------- #


def test_cada_sincronizacion_archiva_el_zip_original_con_su_hash(
    cliente_r2: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    sire: FakeSireClient,
    almacenamiento,
    empresa_principal: Company,
) -> None:
    import hashlib

    contenido = construir_zip_propuesta([_fila()])
    sire.zips = [contenido]

    cuerpo = _sincronizar(cliente_r2, auth).json()

    assert cuerpo["nuevos"] == 1
    assert len(cuerpo["evidencias"]) == 1
    evidencia = cuerpo["evidencias"][0]
    assert evidencia["nombre"] == f"{RUC_PRINCIPAL}-RVIE-202609-{sire.ticket}-1.zip"
    assert evidencia["tipo"] == "zip"
    assert evidencia["comprobante_id"] is None
    # El hash es el del archivo exacto que entregó SUNAT.
    assert evidencia["hash_sha256"] == hashlib.sha256(contenido).hexdigest()
    assert almacenamiento.subidas == [
        {"nombre": evidencia["nombre"], "carpeta": f"sire/{RUC_PRINCIPAL}/202609"}
    ]
    guardado = uow.archivos.archivos[0]
    assert guardado.company_id == empresa_principal.id
    assert guardado.storage_key == f"sire/{RUC_PRINCIPAL}/202609/{evidencia['nombre']}"


def test_un_zip_particionado_archiva_cada_parte(
    cliente_r2: TestClient, auth: dict[str, str], sire: FakeSireClient, almacenamiento
) -> None:
    sire.zips = [
        construir_zip_propuesta([_fila()]),
        construir_zip_propuesta([_fila(**{NRO: "2"})]),
    ]

    cuerpo = _sincronizar(cliente_r2, auth).json()

    assert [e["nombre"][-6:] for e in cuerpo["evidencias"]] == ["-1.zip", "-2.zip"]
    assert len(almacenamiento.subidas) == 2


def test_las_evidencias_se_listan_y_cualquier_miembro_las_ve(
    cliente_r2: TestClient,
    auth: dict[str, str],
    auth_lector: dict[str, str],
    sire: FakeSireClient,
) -> None:
    sire.zips = [construir_zip_propuesta([_fila()])]
    _sincronizar(cliente_r2, auth)
    url = f"/api/v1/companies/{RUC_PRINCIPAL}/sire/evidencias"

    como_lector = cliente_r2.get(url, headers=auth_lector)

    assert como_lector.status_code == 200
    assert como_lector.json()["total"] == 1
    assert como_lector.json()["items"][0]["nombre"].startswith(f"{RUC_PRINCIPAL}-RVIE-202609")
    # Y se descarga con la URL firmada de siempre.
    archivo_id = como_lector.json()["items"][0]["id"]
    firmada = cliente_r2.post(f"/api/v1/archivos/{archivo_id}/download-url", headers=auth_lector)
    assert firmada.status_code == 200


def test_listar_evidencias_sin_token_o_de_otra_empresa(
    client: TestClient, auth: dict[str, str]
) -> None:
    assert client.get(f"/api/v1/companies/{RUC_PRINCIPAL}/sire/evidencias").status_code == 401
    ajena = client.get("/api/v1/companies/20999999999/sire/evidencias", headers=auth)
    assert ajena.status_code in (403, 404)


def test_si_r2_falla_los_comprobantes_se_registran_igual(
    cliente_r2: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    sire: FakeSireClient,
    almacenamiento,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.errors import StorageError

    async def _falla(*_a, **_k):
        raise StorageError("R2 caído")

    monkeypatch.setattr(almacenamiento, "upload_file", _falla)
    sire.zips = [construir_zip_propuesta([_fila()])]

    respuesta = _sincronizar(cliente_r2, auth)

    assert respuesta.status_code == 200
    assert respuesta.json()["nuevos"] == 1
    assert respuesta.json()["evidencias"] == []
    assert len(uow.comprobantes.comprobantes) == 1


def test_sin_r2_configurado_se_sincroniza_sin_evidencia(
    client: TestClient, auth: dict[str, str], sire: FakeSireClient
) -> None:
    sire.zips = [construir_zip_propuesta([_fila()])]

    cuerpo = _sincronizar(client, auth).json()

    assert cuerpo["estado"] == "completado"
    assert cuerpo["evidencias"] == []


def test_un_zip_ilegible_no_deja_evidencia(
    cliente_r2: TestClient,
    auth: dict[str, str],
    uow: FakeUnitOfWork,
    sire: FakeSireClient,
    almacenamiento,
) -> None:
    sire.zips = [b"esto no es un zip"]

    assert _sincronizar(cliente_r2, auth).status_code == 502
    assert almacenamiento.subidas == []
    assert not uow.archivos.archivos


def test_un_tipo_de_cambio_cero_no_es_un_tipo_de_cambio() -> None:
    # SIRE informa 0.000 en los comprobantes dados de baja: la base exige tipo_cambio > 0.
    zip_ = construir_zip_propuesta(
        [
            _fila(
                **{
                    "Moneda": "USD",
                    "Est. Comp": "2",
                    "Total CP": "0",
                    "Tipo Cambio": "0.000",
                    "Tipo Doc Identidad": "0",
                }
            ),
            _fila(**{NRO: "2", "Moneda": "USD", "Total CP": "340.60", "Tipo Cambio": "3.406"}),
        ]
    )

    baja, vigente = parse_propuesta_zip(zip_, ruc_esperado=RUC_PRINCIPAL).registros

    assert baja.tipo_cambio is None and baja.monto_pen is None
    assert vigente.tipo_cambio == Decimal("3.406")
    assert vigente.monto_total == Decimal("100.00") and vigente.monto_pen == Decimal("340.60")
