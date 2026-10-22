"""Automatización diaria: horarios, recuperación, bloqueo, avisos, bitácora e importación."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from scripts import automatizar as au
from scripts.automatizar import Rutas

from app.core.config import Settings
from app.core.enums import OrigenComprobante, RolEmpresa
from app.core.errors import SunatAuthError
from app.core.security import AuthenticatedUser
from app.db.models import Company, CompanyMember
from app.services.results import SireSyncOut
from tests.fakes import FakeUnitOfWork

UBL = Path(__file__).parent / "fixtures" / "ubl"
# F010-00000456 del proveedor 20999999999 a la empresa de pruebas 20123456789.
FACTURA = (UBL / "factura_recibida.xml").read_bytes()
PDF = b"%PDF-1.4 representacion"
CARPETA = "2026/10/20999999999/F010-00000456"


# --------------------------------------------------------------------------- #
# Horarios y recuperación
# --------------------------------------------------------------------------- #
def test_los_meses_a_sincronizar_son_el_actual_y_el_anterior() -> None:
    assert au.meses_a_sincronizar(date(2026, 10, 22)) == ["202610", "202609"]
    assert au.meses_a_sincronizar(date(2026, 1, 5)) == ["202601", "202512"]


def test_sire_se_hace_una_vez_al_dia_desde_las_9() -> None:
    ayer_ok = {"ultima_sire_ok": "2026-10-21T09:01:00"}
    hoy_ok = {"ultima_sire_ok": "2026-10-22T09:01:00"}

    assert au.sire_toca({}, datetime(2026, 10, 22, 8, 59)) is False  # antes de la hora
    assert au.sire_toca({}, datetime(2026, 10, 22, 9, 0)) is True  # primera vez
    assert au.sire_toca(ayer_ok, datetime(2026, 10, 22, 9, 0)) is True
    assert au.sire_toca(hoy_ok, datetime(2026, 10, 22, 13, 0)) is False  # ya se hizo hoy


def test_si_la_pc_estaba_apagada_a_las_9_la_siguiente_corrida_recupera_sire() -> None:
    ayer_ok = {"ultima_sire_ok": "2026-10-21T09:01:00"}

    assert au.sire_toca(ayer_ok, datetime(2026, 10, 22, 13, 5)) is True
    assert au.sire_toca(ayer_ok, datetime(2026, 10, 22, 17, 5)) is True


def test_los_pdf_sueltos_se_reintentan_cada_semana() -> None:
    assert au.sueltos_toca({}, datetime(2026, 10, 22)) is True
    assert (
        au.sueltos_toca({"ultimo_reintento_sueltos": "2026-10-20T09:00:00"}, datetime(2026, 10, 22))
        is False
    )
    assert (
        au.sueltos_toca({"ultimo_reintento_sueltos": "2026-10-10T09:00:00"}, datetime(2026, 10, 22))
        is True
    )


# --------------------------------------------------------------------------- #
# Bloqueo, avisos y bitácora
# --------------------------------------------------------------------------- #
def test_no_arrancan_dos_corridas_a_la_vez_y_un_bloqueo_viejo_se_ignora(tmp_path: Path) -> None:
    ruta = tmp_path / "x.lock"
    ahora = datetime(2026, 10, 22, 9, 0)

    assert au.adquirir_bloqueo(ruta, ahora) is True
    assert au.adquirir_bloqueo(ruta, datetime(2026, 10, 22, 9, 30)) is False
    assert au.adquirir_bloqueo(ruta, datetime(2026, 10, 22, 12, 30)) is True  # el anterior caducó
    au.liberar_bloqueo(ruta)
    assert not ruta.exists()


def test_solo_hay_aviso_si_hay_novedades_o_errores() -> None:
    assert au.decidir_aviso({"errores": [], "novedades": {"compras nuevas": 0}}) is None
    titulo, texto = au.decidir_aviso(
        {"errores": [], "novedades": {"compras nuevas": 3, "PDF unidos": 0}}
    )
    assert "datos nuevos" in titulo and texto == "3 compras nuevas."
    titulo, texto = au.decidir_aviso(
        {"errores": ["SIRE ventas 202610: sunat_auth_error"], "novedades": {"PDF unidos": 2}}
    )
    assert "errores" in titulo and "sunat_auth_error" in texto and "2 PDF unidos" in texto


def test_cada_corrida_deja_constancia_en_la_bitacora_y_en_el_registro(tmp_path: Path) -> None:
    rutas = Rutas(tmp_path / "_automatizacion")
    ahora = datetime(2026, 10, 22, 9, 5)

    au.registrar_actividad(
        rutas, ahora, {"modo": "REAL", "novedades": {"PDF unidos": 2}, "errores": []}
    )
    au.registrar_actividad(rutas, ahora, {"modo": "REAL", "novedades": {}, "errores": ["boom"]})

    lineas = [json.loads(x) for x in rutas.bitacora(ahora).read_text(encoding="utf-8").splitlines()]
    assert [x["errores"] for x in lineas] == [[], ["boom"]]
    log = rutas.log.read_text(encoding="utf-8").splitlines()
    assert log[0] == "[2026-10-22 09:05] REAL: 2 PDF unidos"
    assert log[1] == "[2026-10-22 09:05] REAL: sin novedades | ERRORES: boom"


def test_el_aviso_de_windows_no_rompe_si_falla(monkeypatch: pytest.MonkeyPatch) -> None:
    def falla(*_a, **_k):
        raise OSError("sin powershell")

    monkeypatch.setattr(au.subprocess, "run", falla)

    assert au.avisar_en_pantalla("t", "texto con 'comillas' & símbolos") is False
    assert "''" in au._toast_ps("t", "it's")  # las comillas se escapan para PowerShell


# --------------------------------------------------------------------------- #
# Importación de lo que trajo el correo
# --------------------------------------------------------------------------- #
@pytest.fixture
def entorno(uow: FakeUnitOfWork, empresa_principal: Company, settings: Settings, tmp_path: Path):
    usuario = AuthenticatedUser(id=uuid4())
    acceso = CompanyMember(company=empresa_principal, rol=RolEmpresa.emisor)

    @asynccontextmanager
    async def fabrica(_claims):
        yield uow

    return {
        "uow": uow,
        "fabrica": fabrica,
        "settings": settings,
        "usuario": usuario,
        "acceso": acceso,
        "destino": tmp_path,
        "rutas": Rutas(tmp_path / "_automatizacion"),
    }


def _importar(entorno, **extra):
    return asyncio.run(
        au.importar_pendientes(
            entorno["fabrica"],
            entorno["settings"],
            entorno["usuario"],
            entorno["acceso"],
            entorno["destino"],
            entorno["rutas"],
            **extra,
        )
    )


def _carpeta(entorno, relativa: str, **archivos: bytes) -> None:
    carpeta = entorno["destino"] / relativa
    carpeta.mkdir(parents=True, exist_ok=True)
    for nombre, datos in archivos.items():
        (carpeta / nombre).write_bytes(datos)


def test_se_importa_lo_pendiente_y_la_lista_queda_vacia(entorno) -> None:
    _carpeta(entorno, CARPETA, **{"f.xml": FACTURA})
    au._escribir_json(entorno["rutas"].pendientes, {"carpetas": [CARPETA], "sin_clasificar": []})

    informe = _importar(entorno)

    assert informe["errores"] == []
    assert informe["nuevas"] == 1
    (compra,) = entorno["uow"].comprobantes.comprobantes.values()
    assert compra.origen is OrigenComprobante.recibido and compra.ruc_emisor == "20999999999"
    assert json.loads(entorno["rutas"].pendientes.read_text(encoding="utf-8")) == {
        "carpetas": [],
        "sin_clasificar": [],
    }


def test_si_el_almacenamiento_no_responde_la_carpeta_queda_pendiente(entorno) -> None:
    # Sin R2 el PDF no se puede archivar: no es un éxito a medias, se reintenta después.
    _carpeta(entorno, CARPETA, **{"f.xml": FACTURA, "f.pdf": PDF})
    au._escribir_json(entorno["rutas"].pendientes, {"carpetas": [CARPETA], "sin_clasificar": []})

    informe = _importar(entorno)

    assert any("no_archivado" in e for e in informe["errores"])
    assert json.loads(entorno["rutas"].pendientes.read_text(encoding="utf-8"))["carpetas"] == [
        CARPETA
    ]


def test_repetir_la_importacion_no_duplica(entorno) -> None:
    _carpeta(entorno, CARPETA, **{"f.xml": FACTURA})
    au._escribir_json(entorno["rutas"].pendientes, {"carpetas": [CARPETA], "sin_clasificar": []})
    _importar(entorno)
    au._escribir_json(entorno["rutas"].pendientes, {"carpetas": [CARPETA], "sin_clasificar": []})

    informe = _importar(entorno)

    assert informe["repetidas"] == 1 and informe["nuevas"] == 0
    assert len(entorno["uow"].comprobantes.comprobantes) == 1


def test_lo_que_falla_se_informa_y_queda_pendiente_para_la_proxima(entorno) -> None:
    ajena = FACTURA.replace(b"20123456789", b"20555555555")  # compra de otra empresa
    _carpeta(entorno, CARPETA, **{"f.xml": ajena})
    au._escribir_json(entorno["rutas"].pendientes, {"carpetas": [CARPETA], "sin_clasificar": []})

    informe = _importar(entorno)

    assert len(informe["errores"]) == 1 and "invalid_document" in informe["errores"][0]
    assert json.loads(entorno["rutas"].pendientes.read_text(encoding="utf-8"))["carpetas"] == [
        CARPETA
    ]


def test_una_carpeta_con_problemas_no_frena_a_las_demas(entorno) -> None:
    otra = "2026/10/20999999999/F010-00000457"
    _carpeta(entorno, CARPETA, **{"f.xml": FACTURA.replace(b"20123456789", b"20555555555")})
    _carpeta(entorno, otra, **{"f.xml": FACTURA.replace(b"F010-00000456", b"F010-00000457")})
    au._escribir_json(
        entorno["rutas"].pendientes, {"carpetas": [CARPETA, otra], "sin_clasificar": []}
    )

    informe = _importar(entorno)

    assert informe["nuevas"] == 1 and len(informe["errores"]) == 1
    assert json.loads(entorno["rutas"].pendientes.read_text(encoding="utf-8"))["carpetas"] == [
        CARPETA
    ]


def test_en_simulacion_no_se_escribe_nada(entorno) -> None:
    _carpeta(entorno, CARPETA, **{"f.xml": FACTURA})
    au._escribir_json(entorno["rutas"].pendientes, {"carpetas": [CARPETA], "sin_clasificar": []})

    informe = _importar(entorno, simular=True)

    assert informe["carpetas_pendientes"] == 1
    assert entorno["uow"].comprobantes.comprobantes == {}
    assert json.loads(entorno["rutas"].pendientes.read_text(encoding="utf-8"))["carpetas"] == [
        CARPETA
    ]


# --------------------------------------------------------------------------- #
# SIRE
# --------------------------------------------------------------------------- #
def _salida(estado: str, nuevos: int = 0, ticket: str | None = None) -> SireSyncOut:
    return SireSyncOut(periodo="202610", estado=estado, ticket=ticket, nuevos=nuevos, mensaje="x")


def test_sire_retoma_el_ticket_cuando_sunat_tarda(entorno, monkeypatch: pytest.MonkeyPatch) -> None:
    llamadas: list[tuple[str, str | None]] = []

    async def ventas(**kw):
        llamadas.append(("ventas", kw["ticket"]))
        return (
            _salida("en_proceso", ticket="T1") if kw["ticket"] is None else _salida("completado", 5)
        )

    async def compras(**kw):
        llamadas.append(("compras", kw["ticket"]))
        return _salida("completado", 7)

    monkeypatch.setattr(au, "sincronizar_ventas", ventas)
    monkeypatch.setattr(au, "sincronizar_compras", compras)
    esperas: list[int] = []

    async def dormir(segundos):
        esperas.append(segundos)

    informe = asyncio.run(
        au.sincronizar_sire(
            entorno["fabrica"],
            entorno["settings"],
            entorno["usuario"],
            entorno["acceso"],
            ["202610"],
            dormir=dormir,
        )
    )

    assert informe["errores"] == []
    assert (informe["ventas_nuevas"], informe["compras_nuevas"]) == (5, 7)
    assert llamadas == [("ventas", None), ("ventas", "T1"), ("compras", None)]
    assert esperas == [au.ESPERA_TICKET_SEGUNDOS]


def test_un_error_de_sire_se_informa_y_los_demas_meses_siguen(
    entorno, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def ventas(**kw):
        if kw["periodo"] == "202610":
            raise SunatAuthError("SUNAT rechazó las credenciales")
        return _salida("completado", 2)

    async def compras(**kw):
        return _salida("sin_datos")

    monkeypatch.setattr(au, "sincronizar_ventas", ventas)
    monkeypatch.setattr(au, "sincronizar_compras", compras)

    informe = asyncio.run(
        au.sincronizar_sire(
            entorno["fabrica"],
            entorno["settings"],
            entorno["usuario"],
            entorno["acceso"],
            ["202610", "202609"],
        )
    )

    assert len(informe["errores"]) == 1 and "ventas 202610" in informe["errores"][0]
    assert informe["ventas_nuevas"] == 2  # el mes anterior sí se sincronizó


def test_en_simulacion_sire_no_llama_a_sunat(entorno, monkeypatch: pytest.MonkeyPatch) -> None:
    async def prohibido(**_kw):
        raise AssertionError("no debe llamar a SUNAT en simulación")

    monkeypatch.setattr(au, "sincronizar_ventas", prohibido)
    monkeypatch.setattr(au, "sincronizar_compras", prohibido)

    informe = asyncio.run(
        au.sincronizar_sire(
            entorno["fabrica"],
            entorno["settings"],
            entorno["usuario"],
            entorno["acceso"],
            ["202610"],
            simular=True,
        )
    )

    assert informe["errores"] == [] and informe["ventas_nuevas"] == 0


# --------------------------------------------------------------------------- #
# Registro de la corrida en la base (apartado Logs)
# --------------------------------------------------------------------------- #
def test_el_resultado_de_la_corrida_distingue_errores_novedades_y_nada() -> None:
    assert au.resultado_de({"errores": ["x"], "novedades": {"a": 3}}) == "con_errores"
    assert au.resultado_de({"errores": [], "novedades": {"a": 3, "b": 0}}) == "con_novedades"
    assert au.resultado_de({"errores": [], "novedades": {"a": 0}}) == "ok"


def test_la_corrida_se_guarda_con_su_estado_y_sus_pendientes(entorno) -> None:
    resumen = {
        "tareas": ["sire", "correo"],
        "errores": [],
        "novedades": {"PDF unidos": 2},
        "advertencias": ["usó el primer administrador"],
        "sire": {"meses": ["202610"]},
        "importacion": {"nuevas": 1},
        "segundos": 99,  # lo calcula la corrida, no se copia
    }
    registro = au.armar_log(
        resumen,
        acceso=entorno["acceso"],
        usuario=entorno["usuario"],
        estado={"ultima_sire_ok": "2026-10-22T09:01:00"},
        pendientes={"carpetas": ["2026/10/x/y"]},
        segundos=53,
    )

    asyncio.run(au.guardar_log(entorno["fabrica"], entorno["usuario"], entorno["acceso"], registro))

    (log,) = entorno["uow"].automatizacion_logs.logs
    assert (log.resultado, log.modo, log.duracion_segundos) == ("con_novedades", "real", 53)
    assert log.tareas == ["sire", "correo"] and log.novedades == {"PDF unidos": 2}
    assert log.detalle == {"sire": {"meses": ["202610"]}, "importacion": {"nuevas": 1}}
    assert log.estado == {"ultima_sire_ok": "2026-10-22T09:01:00"}
    assert log.pendientes == {"carpetas": ["2026/10/x/y"]}
    assert log.created_by == entorno["usuario"].id
