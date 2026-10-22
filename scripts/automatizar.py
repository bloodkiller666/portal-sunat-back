"""Automatiza SIRE, el correo de proveedores y la importación al portal, sin pulsar botones.

Pensado para la Tarea programada de Windows (ver `scripts/registrar_tarea_windows.ps1`), que lo
ejecuta 3 veces al día. En cada corrida (`--tarea auto`):

1. **SIRE** (ventas y compras, mes actual y anterior): una vez al día, a partir de las 9:00. Si la
   PC estaba apagada a esa hora, la primera corrida posterior lo hace. Es solo lectura hacia SUNAT
   y no duplica nada (con `forzar` solo agrega lo nuevo).
2. **Correo**: extrae los adjuntos de los correos que aún no vio (todos los pendientes, no solo los
   de hoy: si la PC estuvo apagada, se acumulan y se bajan en la siguiente corrida).
3. **Importación**: sube al portal lo que el correo trajo (completa lo de SIRE, une PDF y CDR, y
   no duplica). Lo que falla queda pendiente y se reintenta en la corrida siguiente.
4. **Avisos y bitácora**: cada corrida deja una línea en la bitácora; solo hay aviso en pantalla
   cuando se registró algo nuevo o hubo errores.

Sin `--aplicar` NO escribe nada (ni en la base ni en SUNAT): solo muestra qué haría.

    uv run --with pywin32 python -m scripts.automatizar --tarea auto             # simulación
    uv run --with pywin32 python -m scripts.automatizar --tarea auto --aplicar   # de verdad

Archivos (en `--destino`, por defecto `~/Downloads/FacturasProveedores/_automatizacion/`):
`automatizacion.log` (legible), `bitacora-AAAA-MM.jsonl` (una línea JSON por corrida),
`estado.json`, `pendientes_importar.json` y `automatizar.lock`.

El usuario con el que queda registrado lo importado sale de `--usuario-id` o de la variable
`AUTOMATIZACION_USER_ID` (conviene un usuario «Automatización» con rol emisor); sin él se usa el
primer administrador de la empresa y se deja constancia en la bitácora.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.redaction import redact
from app.core.security import AuthenticatedUser
from app.db.models import AutomatizacionLogRegistro, CompanyMember
from app.db.pool import Database
from app.db.uow import build_uow_factory
from app.main import crear_cliente_sire
from app.secrets.factory import build_secrets_provider
from app.services.companies import resolve_company_access
from app.services.proveedores_paquete import importar_archivos_proveedores
from app.services.sire import sincronizar_compras, sincronizar_ventas

# Cortes de conexión con la base (el pooler cierra las conexiones inactivas, p. ej. mientras se
# lee el correo durante minutos): no son un fallo de los datos, se reintentan.
ERRORES_TRANSITORIOS = (
    asyncpg.InterfaceError,
    asyncpg.PostgresConnectionError,
    TimeoutError,
    ConnectionError,
)
INTENTOS_BASE = 3
ESPERA_REINTENTO_SEGUNDOS = 5
HORA_SIRE_POR_DEFECTO = 9
ESPERA_TICKET_SEGUNDOS = 20
MAX_REINTENTOS_TICKET = 12
BLOQUEO_CADUCA_HORAS = 3
REINTENTO_SUELTOS_DIAS = 7
EXTENSIONES = {".xml", ".pdf"}


@dataclass(frozen=True)
class Rutas:
    """Archivos de trabajo de la automatización."""

    base: Path

    @property
    def log(self) -> Path:
        return self.base / "automatizacion.log"

    @property
    def estado(self) -> Path:
        return self.base / "estado.json"

    @property
    def pendientes(self) -> Path:
        return self.base / "pendientes_importar.json"

    @property
    def bloqueo(self) -> Path:
        return self.base / "automatizar.lock"

    @property
    def sueltos_intentados(self) -> Path:
        return self.base / "pdfs_sueltos_intentados.json"

    def bitacora(self, ahora: datetime) -> Path:
        return self.base / f"bitacora-{ahora:%Y-%m}.jsonl"


# --------------------------------------------------------------------------- #
# Lógica pura (se prueba sin Outlook, base ni SUNAT)
# --------------------------------------------------------------------------- #
async def con_reintentos[T](
    operacion: Callable[[], Awaitable[T]],
    *,
    intentos: int = INTENTOS_BASE,
    espera: float = ESPERA_REINTENTO_SEGUNDOS,
    dormir: Any = asyncio.sleep,
) -> T:
    """Ejecuta `operacion` y la repite si la conexión con la base se cortó.

    Cada intento abre su propia transacción, así que repetirla es seguro: si la anterior se cortó,
    se deshizo. Cualquier otro error (datos, permisos…) se propaga de inmediato.
    """
    for intento in range(1, intentos + 1):
        try:
            return await operacion()
        except ERRORES_TRANSITORIOS:
            if intento == intentos:
                raise
            await dormir(espera * intento)
    raise AssertionError("inalcanzable")  # pragma: no cover


def describir_error(exc: BaseException) -> str:
    """Texto útil para la bitácora: tipo, restricción violada (si la hay) y un resumen."""
    restriccion = getattr(exc, "constraint_name", None)
    detalle = redact(str(exc)).strip().splitlines()[0][:120] if str(exc).strip() else ""
    return ": ".join(x for x in (type(exc).__name__, restriccion, detalle) if x)


def meses_a_sincronizar(hoy: date) -> list[str]:
    """Mes actual y anterior (`AAAAMM`): SIRE sigue añadiendo comprobantes a ese último."""
    anterior = hoy.replace(day=1) - timedelta(days=1)
    return [f"{hoy:%Y%m}", f"{anterior:%Y%m}"]


def _leer_json(ruta: Path, defecto: Any) -> Any:
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defecto


def _escribir_json(ruta: Path, datos: Any) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")


def sire_toca(estado: dict[str, Any], ahora: datetime, hora: int = HORA_SIRE_POR_DEFECTO) -> bool:
    """¿Hay que sincronizar SIRE? Una vez al día, desde `hora`; recupera un día perdido."""
    if ahora.hour < hora:
        return False
    ultima = estado.get("ultima_sire_ok")
    return ultima is None or datetime.fromisoformat(ultima).date() < ahora.date()


def sueltos_toca(estado: dict[str, Any], ahora: datetime) -> bool:
    """¿Toca reintentar todos los PDF sueltos? Cuando llegan XML tarde pueden ya identificarse."""
    ultima = estado.get("ultimo_reintento_sueltos")
    return ultima is None or ahora - datetime.fromisoformat(ultima) >= timedelta(
        days=REINTENTO_SUELTOS_DIAS
    )


def adquirir_bloqueo(ruta: Path, ahora: datetime) -> bool:
    """Evita dos corridas a la vez (p. ej. una larga y la siguiente del horario)."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    if ruta.exists():
        try:
            desde = datetime.fromisoformat(ruta.read_text(encoding="utf-8").strip())
        except ValueError:
            desde = ahora - timedelta(days=1)
        if ahora - desde < timedelta(hours=BLOQUEO_CADUCA_HORAS):
            return False  # otra corrida sigue activa
    ruta.write_text(ahora.isoformat(), encoding="utf-8")
    return True


def liberar_bloqueo(ruta: Path) -> None:
    ruta.unlink(missing_ok=True)


def decidir_aviso(resumen: dict[str, Any]) -> tuple[str, str] | None:
    """`(título, texto)` del aviso en pantalla, o `None` si no hubo novedades ni errores."""
    errores = resumen.get("errores", [])
    nuevos = resumen.get("novedades", {})
    partes = [f"{n} {texto}" for texto, n in nuevos.items() if n]
    if errores:
        detalle = "; ".join(errores[:2]) + ("…" if len(errores) > 2 else "")
        extra = f" Además: {', '.join(partes)}." if partes else ""
        return "Automatización: hubo errores", f"{detalle}{extra} Revisa la bitácora."
    if partes:
        return "Automatización: datos nuevos", ", ".join(partes).capitalize() + "."
    return None


def registrar_actividad(rutas: Rutas, ahora: datetime, resumen: dict[str, Any]) -> None:
    """Deja la corrida en la bitácora (JSON por línea) y en el registro legible."""
    rutas.base.mkdir(parents=True, exist_ok=True)
    linea = {"cuando": ahora.isoformat(timespec="seconds"), **resumen}
    with rutas.bitacora(ahora).open("a", encoding="utf-8") as archivo:
        archivo.write(json.dumps(linea, ensure_ascii=False) + "\n")
    novedades = ", ".join(f"{n} {t}" for t, n in resumen.get("novedades", {}).items() if n)
    errores = resumen.get("errores", [])
    texto = f"[{ahora:%Y-%m-%d %H:%M}] {resumen.get('modo', '')}: "
    texto += novedades or "sin novedades"
    if errores:
        texto += f" | ERRORES: {' | '.join(errores)}"
    with rutas.log.open("a", encoding="utf-8") as archivo:
        archivo.write(texto + "\n")


def _toast_ps(titulo: str, texto: str) -> str:
    def esc(valor: str) -> str:
        return valor.replace("'", "''").replace("&", "y").replace("<", "").replace(">", "")

    return (
        "$t=[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,"
        "ContentType=WindowsRuntime];"
        "$x=$t::GetTemplateContent('ToastText02');"
        "$n=$x.GetElementsByTagName('text');"
        f"$n.Item(0).AppendChild($x.CreateTextNode('{esc(titulo)}'))|Out-Null;"
        f"$n.Item(1).AppendChild($x.CreateTextNode('{esc(texto)[:220]}'))|Out-Null;"
        "$toast=[Windows.UI.Notifications.ToastNotification]::new($x);"
        "$t::CreateToastNotifier('Portal SUNAT').Show($toast)"
    )


def avisar_en_pantalla(titulo: str, texto: str) -> bool:
    """Notificación de Windows (mejor esfuerzo): un fallo del aviso nunca rompe la corrida."""
    if sys.platform != "win32":
        return False
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _toast_ps(titulo, texto)],
            check=True,
            capture_output=True,
            timeout=30,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


# --------------------------------------------------------------------------- #
# Importación de lo que trajo el correo
# --------------------------------------------------------------------------- #
INFORMATIVOS = frozenset(
    {
        "conflict",
        "sin_xml",
        "guia",
        "cdr_sin_comprobante",
        "cdr_repetido",
        "xml_no_reconocido",
        "tipo_no_soportado",
        "sin_comprobante",
        "ambiguo",
        "xml_con_error",
    }
)


def _huella(datos: bytes) -> str:
    import hashlib

    return hashlib.sha256(datos).hexdigest()[:20]


async def _importar_grupo(
    uow_factory: Any,
    settings: Settings,
    usuario: AuthenticatedUser,
    acceso: CompanyMember,
    archivos: list[tuple[str, bytes]],
) -> Any:
    """Importa un grupo de archivos en SU transacción (si se corta la conexión, se deshace)."""
    async with uow_factory(usuario.claims_para_rls) as uow:
        return await importar_archivos_proveedores(
            uow=uow, settings=settings, user=usuario, access=acceso, archivos=archivos
        )


async def importar_pendientes(
    uow_factory: Any,
    settings: Settings,
    usuario: AuthenticatedUser,
    acceso: CompanyMember,
    destino: Path,
    rutas: Rutas,
    *,
    reintentar_sueltos: bool = False,
    simular: bool = False,
    espera: float = ESPERA_REINTENTO_SEGUNDOS,
) -> dict[str, Any]:
    """Importa las carpetas con novedades (una transacción por carpeta) y los PDF sueltos nuevos."""
    pend = _leer_json(rutas.pendientes, {})
    carpetas = list(pend.get("carpetas", []))
    sueltas = list(pend.get("sin_clasificar", []))
    if reintentar_sueltos:
        base = destino / "_sin_clasificar"
        sueltas = sorted({p.parent.relative_to(destino).as_posix() for p in base.rglob("*.pdf")})
    informe: dict[str, Any] = {
        "carpetas_pendientes": len(carpetas),
        "completadas": 0,
        "nuevas": 0,
        "repetidas": 0,
        "pdf_guardados": 0,
        "cdr_guardados": 0,
        "pdf_sueltos_unidos": 0,
        "errores": [],
    }
    if simular:
        return informe

    quedan_carpetas: list[str] = []
    for carpeta in carpetas:
        ruta = destino / carpeta
        archivos = [
            (f.name, f.read_bytes())
            for f in sorted(ruta.glob("*"))
            if f.suffix.lower() in EXTENSIONES
        ]
        if not archivos:
            continue
        try:
            r = await con_reintentos(
                partial(_importar_grupo, uow_factory, settings, usuario, acceso, archivos),
                espera=espera,
            )
        except Exception as exc:
            informe["errores"].append(f"{carpeta}: {describir_error(exc)}")
            quedan_carpetas.append(carpeta)
            continue
        informe["completadas"] += r.completadas
        informe["nuevas"] += r.nuevas
        informe["repetidas"] += r.duplicados
        informe["pdf_guardados"] += sum(
            1 for i in r.items if i.tipo == "pdf" and i.ok and i.situacion == "guardado"
        )
        informe["cdr_guardados"] += sum(
            1 for i in r.items if i.tipo == "cdr" and i.ok and i.situacion == "guardado"
        )
        fallos = [i for i in r.items if not i.ok and i.code not in INFORMATIVOS]
        for i in fallos:
            informe["errores"].append(f"{carpeta}/{i.archivo}: {i.code} {redact(i.mensaje or '')}")
        if fallos:
            quedan_carpetas.append(carpeta)

    intentados = set(_leer_json(rutas.sueltos_intentados, []))
    for carpeta in sueltas:
        for pdf in sorted((destino / carpeta).glob("*.pdf")):
            datos = pdf.read_bytes()
            huella = _huella(datos)
            if huella in intentados and not reintentar_sueltos:
                continue
            try:
                r = await con_reintentos(
                    partial(
                        _importar_grupo, uow_factory, settings, usuario, acceso, [(pdf.name, datos)]
                    ),
                    espera=espera,
                )
            except Exception as exc:
                informe["errores"].append(f"{carpeta}/{pdf.name}: {describir_error(exc)}")
                continue
            intentados.add(huella)
            informe["pdf_sueltos_unidos"] += sum(
                1 for i in r.items if i.ok and i.situacion == "unido"
            )
    _escribir_json(rutas.sueltos_intentados, sorted(intentados))
    _escribir_json(rutas.pendientes, {"carpetas": quedan_carpetas, "sin_clasificar": []})
    return informe


# --------------------------------------------------------------------------- #
# SIRE
# --------------------------------------------------------------------------- #
async def _sincronizar_un_periodo(
    uow_factory: Any, usuario: AuthenticatedUser, funcion: Any, **kwargs: Any
) -> Any:
    async with uow_factory(usuario.claims_para_rls) as uow:
        return await funcion(uow=uow, user=usuario, **kwargs)


async def sincronizar_sire(
    uow_factory: Any,
    settings: Settings,
    usuario: AuthenticatedUser,
    acceso: CompanyMember,
    meses: list[str],
    *,
    simular: bool = False,
    dormir: Any = asyncio.sleep,
    espera: float = ESPERA_REINTENTO_SEGUNDOS,
) -> dict[str, Any]:
    """Ventas y compras de cada mes, retomando el ticket si SUNAT tarda."""
    informe: dict[str, Any] = {
        "ventas_nuevas": 0,
        "compras_nuevas": 0,
        "errores": [],
        "meses": meses,
    }
    if simular:
        return informe
    proveedor = build_secrets_provider(settings)
    fabrica = crear_cliente_sire()
    for libro, funcion, clave in (
        ("ventas", sincronizar_ventas, "ventas_nuevas"),
        ("compras", sincronizar_compras, "compras_nuevas"),
    ):
        for periodo in meses:
            ticket: str | None = None
            for _ in range(MAX_REINTENTOS_TICKET):
                try:
                    r = await con_reintentos(
                        partial(
                            _sincronizar_un_periodo,
                            uow_factory,
                            usuario,
                            funcion,
                            settings=settings,
                            secrets_provider=proveedor,
                            client_factory=fabrica,
                            access=acceso,
                            periodo=periodo,
                            ticket=ticket,
                            forzar=True,
                        ),
                        espera=espera,
                        dormir=dormir,
                    )
                except AppError as exc:
                    informe["errores"].append(
                        f"SIRE {libro} {periodo}: {exc.code} {redact(exc.message)}"
                    )
                    break
                except Exception as exc:
                    informe["errores"].append(f"SIRE {libro} {periodo}: {describir_error(exc)}")
                    break
                if r.estado == "en_proceso" and r.ticket:
                    ticket = r.ticket
                    await dormir(ESPERA_TICKET_SEGUNDOS)
                    continue
                informe[clave] += r.nuevos
                break
            else:
                informe["errores"].append(
                    f"SIRE {libro} {periodo}: SUNAT sigue generando el archivo"
                )
    return informe


# --------------------------------------------------------------------------- #
# Registro de la corrida en la base (apartado «Logs» del portal)
# --------------------------------------------------------------------------- #
def resultado_de(resumen: dict[str, Any]) -> str:
    if resumen.get("errores"):
        return "con_errores"
    return "con_novedades" if any(resumen.get("novedades", {}).values()) else "ok"


def armar_log(
    resumen: dict[str, Any],
    *,
    acceso: CompanyMember,
    usuario: AuthenticatedUser,
    estado: dict[str, Any],
    pendientes: dict[str, Any],
    segundos: int,
) -> AutomatizacionLogRegistro:
    """Lo que ve el portal de una corrida real: resumen, errores y una foto del estado."""
    return AutomatizacionLogRegistro(
        company_id=acceso.company_id,
        modo="real",
        tareas=list(resumen.get("tareas", [])),
        resultado=resultado_de(resumen),  # type: ignore[arg-type]
        duracion_segundos=segundos,
        novedades={k: int(v) for k, v in resumen.get("novedades", {}).items()},
        errores=[str(e) for e in resumen.get("errores", [])],
        advertencias=[str(a) for a in resumen.get("advertencias", [])],
        detalle={k: resumen[k] for k in ("sire", "correo", "importacion") if k in resumen},
        estado=estado,
        pendientes=pendientes,
        created_by=usuario.id,
    )


async def guardar_log(
    uow_factory: Any,
    usuario: AuthenticatedUser,
    acceso: CompanyMember,
    registro: AutomatizacionLogRegistro,
) -> None:
    async def _guardar() -> None:
        async with uow_factory(usuario.claims_para_rls) as uow:
            await uow.automatizacion_logs.registrar(registro)

    await con_reintentos(_guardar)


# --------------------------------------------------------------------------- #
# Corrida completa
# --------------------------------------------------------------------------- #
async def _usuario_de_servicio(
    db: Database, ruc: str, usuario_id: str | None
) -> tuple[AuthenticatedUser, str | None]:
    """Quién queda como autor de lo importado. Devuelve `(usuario, advertencia)`."""
    if usuario_id:
        return AuthenticatedUser(id=UUID(usuario_id)), None
    pool = db._pool
    assert pool is not None
    async with pool.acquire() as conexion:
        fila = await conexion.fetchval(
            """
            select uc.user_id from public.user_companies uc
              join public.companies c on c.id = uc.company_id
             where c.ruc = $1 and uc.activo and uc.rol in ('admin', 'emisor')
             order by uc.rol, uc.user_id limit 1
            """,
            ruc,
        )
    if fila is None:
        raise RuntimeError("No hay un usuario admin/emisor para la empresa")
    return AuthenticatedUser(id=fila), (
        "Sin AUTOMATIZACION_USER_ID: se usó el primer administrador; conviene crear un usuario "
        "«Automatización»"
    )


async def ejecutar_corrida(args: argparse.Namespace, ahora: datetime) -> dict[str, Any]:
    inicio = time.monotonic()
    destino = Path(args.destino)  # ya resuelto en main()
    rutas = Rutas(destino / "_automatizacion")
    resumen: dict[str, Any] = {
        "modo": "REAL" if args.aplicar else "simulación",
        "tareas": [],
        "errores": [],
        "novedades": {},
    }
    estado = _leer_json(rutas.estado, {})
    hacer_sire = args.tarea == "sire" or (
        args.tarea == "auto" and sire_toca(estado, ahora, args.hora_sire)
    )
    hacer_correo = args.tarea in ("correo", "auto")
    resumen["tareas"] = [t for t, h in (("sire", hacer_sire), ("correo", hacer_correo)) if h]

    settings = get_settings()
    db = Database(settings)
    await db.connect()
    try:
        uow_factory = build_uow_factory(db)
        usuario, advertencia = await _usuario_de_servicio(
            db, args.ruc, args.usuario_id or os.environ.get("AUTOMATIZACION_USER_ID")
        )
        if advertencia:
            resumen["advertencias"] = [advertencia]
        async with uow_factory(usuario.claims_para_rls) as uow:
            acceso = await resolve_company_access(uow, usuario, args.ruc)

        if hacer_sire:
            r = await sincronizar_sire(
                uow_factory,
                settings,
                usuario,
                acceso,
                meses_a_sincronizar(ahora.date()),
                simular=not args.aplicar,
            )
            resumen["sire"] = r
            resumen["errores"] += r["errores"]
            resumen["novedades"]["ventas nuevas desde SIRE"] = r["ventas_nuevas"]
            resumen["novedades"]["compras nuevas desde SIRE"] = r["compras_nuevas"]
            if args.aplicar and not r["errores"]:
                estado["ultima_sire_ok"] = ahora.isoformat(timespec="seconds")

        if hacer_correo:
            try:
                from scripts.extraer_correo_outlook import CARPETAS_POR_DEFECTO, extraer

                codigo, ext = await asyncio.to_thread(
                    extraer,
                    argparse.Namespace(
                        buzon=args.buzon,
                        destino=str(destino),
                        ruc=args.ruc,
                        carpetas=CARPETAS_POR_DEFECTO,
                        desde=None,
                        hasta=None,
                        limite=0,
                        simular=not args.aplicar,
                        sin_paquetes=True,
                    ),
                )
                if codigo != 0 or ext is None:
                    resumen["errores"].append(
                        "No se pudo leer el correo (¿Outlook clásico cerrado o sin el buzón?)"
                    )
                else:
                    resumen["correo"] = {
                        k: ext[k]
                        for k in (
                            "revisados",
                            "con_facturas",
                            "comprobantes_de_la_empresa",
                            "errores",
                        )
                    }
                    resumen["novedades"]["compras con archivos del correo"] = ext[
                        "comprobantes_de_la_empresa"
                    ]
                    if ext["errores"]:
                        resumen["errores"].append(f"{ext['errores']} correo(s) no se pudieron leer")
            except Exception as exc:
                resumen["errores"].append(f"Correo: {describir_error(exc)}")

            # Leer el correo tarda minutos con la base inactiva: el pooler suele cerrar esas
            # conexiones, así que se abren de nuevo antes de importar.
            await db.close()
            await db.connect()

            toca_sueltos = args.aplicar and sueltos_toca(estado, ahora)
            imp = await importar_pendientes(
                uow_factory,
                settings,
                usuario,
                acceso,
                destino,
                rutas,
                reintentar_sueltos=toca_sueltos,
                simular=not args.aplicar,
            )
            resumen["importacion"] = imp
            resumen["errores"] += imp["errores"]
            resumen["novedades"]["compras completadas con su XML"] = imp["completadas"]
            resumen["novedades"]["compras nuevas (no estaban en SIRE)"] = imp["nuevas"]
            resumen["novedades"]["PDF unidos"] = imp["pdf_guardados"] + imp["pdf_sueltos_unidos"]
            resumen["novedades"]["CDR guardados"] = imp["cdr_guardados"]
            if toca_sueltos:
                estado["ultimo_reintento_sueltos"] = ahora.isoformat(timespec="seconds")
        if args.aplicar:
            _escribir_json(rutas.estado, estado)
            try:
                registro = armar_log(
                    resumen,
                    acceso=acceso,
                    usuario=usuario,
                    estado=estado,
                    pendientes=_leer_json(rutas.pendientes, {}),
                    segundos=round(time.monotonic() - inicio),
                )
                await guardar_log(uow_factory, usuario, acceso, registro)
            except Exception as exc:
                # El log local (automatizacion.log) igual queda; solo falta verlo en el portal.
                resumen.setdefault("advertencias", []).append(
                    f"No se pudo guardar la corrida en la base ({type(exc).__name__}): "
                    "¿está aplicada la migración 0013?"
                )
    finally:
        await db.close()
    return resumen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SIRE + correo + importación automáticos")
    parser.add_argument("--tarea", choices=["auto", "sire", "correo"], default="auto")
    parser.add_argument("--aplicar", action="store_true", help="Escribe de verdad (si no, simula)")
    parser.add_argument("--ruc", default="20514831620")
    parser.add_argument("--buzon", default="recepcionfacturas")
    parser.add_argument("--destino", default="~/Downloads/FacturasProveedores")
    parser.add_argument("--hora-sire", type=int, default=HORA_SIRE_POR_DEFECTO)
    parser.add_argument("--usuario-id", default=None)
    args = parser.parse_args(argv)

    ahora = datetime.now()
    args.destino = str(Path(args.destino).expanduser().resolve())
    rutas = Rutas(Path(args.destino) / "_automatizacion")
    if not adquirir_bloqueo(rutas.bloqueo, ahora):
        print("Ya hay otra corrida en curso: no se inicia otra.")
        return 0
    inicio = time.monotonic()
    try:
        resumen = asyncio.run(ejecutar_corrida(args, ahora))
    except Exception as exc:
        resumen = {
            "modo": "REAL" if args.aplicar else "simulación",
            "tareas": [],
            "novedades": {},
            "errores": [f"Fallo inesperado: {type(exc).__name__}: {redact(str(exc))[:200]}"],
        }
        traceback.print_exc()
    finally:
        liberar_bloqueo(rutas.bloqueo)
    resumen["segundos"] = round(time.monotonic() - inicio)
    print(json.dumps(resumen, ensure_ascii=False, indent=2, default=str))
    if args.aplicar:
        registrar_actividad(rutas, ahora, resumen)
        aviso = decidir_aviso(resumen)
        if aviso:
            avisar_en_pantalla(*aviso)
    return 1 if resumen["errores"] else 0


if __name__ == "__main__":
    sys.exit(main())
