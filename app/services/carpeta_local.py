"""Carpeta local de respaldo: `Descargas/AAAA/MES/RUC/SERIE-NÚMERO`.

El navegador guarda ahí el XML y el CDR (ver el frontend). Este módulo, cuando el backend corre
en la MISMA PC del usuario, pide al sistema operativo abrirla. Por seguridad nunca recibe una
ruta del cliente: solo nombres de carpeta que se limpian y se limitan a la carpeta base configurada
(`LOCAL_DESCARGAS_DIR` para Ventas, `LOCAL_PROVEEDORES_DIR` para Compras).
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import UUID

from app.core.config import Settings
from app.core.enums import OrigenComprobante, TipoArchivo
from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.meses import carpeta_de_mes, variantes_de_mes
from app.core.security import AuthenticatedUser
from app.db.models import Comprobante
from app.db.repositories.base import UnitOfWork
from app.services.companies import require_membership
from app.services.correo_compras import NOMBRE_CORREO_TXT, texto_correo_de_compra
from app.services.store import documento_del_comprobante
from app.storage.base import StorageProvider
from app.storage.factory import get_storage_provider

logger = get_logger(__name__)

_NO_PERMITIDO = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def _limpio(texto: str, vacio: str) -> str:
    return _NO_PERMITIDO.sub("-", texto).strip(" .") or vacio


def carpeta_base(settings: Settings) -> Path:
    return (settings.local_descargas_dir or Path.home() / "Downloads").resolve()


def carpeta_proveedores(settings: Settings) -> Path:
    return (
        settings.local_proveedores_dir or Path.home() / "Downloads" / "FacturasProveedores"
    ).resolve()


def carpetas_base(settings: Settings, origen: str | None = None) -> list[Path]:
    """Dónde buscar según el apartado: Ventas (`emitido`) o Compras (`recibido`).

    Cada apartado mira solo su carpeta: Ventas la que llena el navegador (`LOCAL_DESCARGAS_DIR`),
    Compras la que llena el extractor del correo (`LOCAL_PROVEEDORES_DIR`). Sin `origen` (clientes
    antiguos) se buscan ambas, primero Ventas.
    """
    ventas, compras = carpeta_base(settings), carpeta_proveedores(settings)
    if origen == "emitido":
        return [ventas]
    if origen == "recibido":
        return [compras]
    return [ventas] if compras == ventas else [ventas, compras]


def _ventanas_del_explorador(titulo: str) -> list[int]:  # pragma: no cover - solo Windows
    """Ventanas del Explorador cuyo título es `titulo`, de la de arriba a la de abajo."""
    import ctypes
    from ctypes import wintypes

    usuario = ctypes.windll.user32
    encontradas: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def revisar(hwnd, _lparam):
        clase = ctypes.create_unicode_buffer(64)
        usuario.GetClassNameW(hwnd, clase, 64)
        if clase.value == "CabinetWClass":
            texto = ctypes.create_unicode_buffer(512)
            usuario.GetWindowTextW(hwnd, texto, 512)
            # En Windows 10/11 el título es «NOMBRE - Explorador de archivos», no solo el nombre.
            if texto.value == titulo or texto.value.startswith(f"{titulo} - "):
                encontradas.append(hwnd)
        return True

    usuario.EnumWindows(revisar, 0)
    return encontradas


def _enfocar(hwnd: int) -> None:  # pragma: no cover - solo Windows, con ventanas reales
    """Restaura (si está minimizada) y pone al frente una ventana, aunque otro proceso tenga foco.

    Windows no deja que un proceso en segundo plano (el backend) pase una ventana al frente: la deja
    minimizada o parpadeando en la barra de tareas. Se combinan los trucos conocidos: pulsar Alt,
    unir el hilo de entrada al de la ventana en primer plano y alternar «siempre encima».
    """
    import ctypes

    usuario = ctypes.windll.user32
    kernel = ctypes.windll.kernel32
    if usuario.IsIconic(hwnd):
        usuario.ShowWindow(hwnd, 9)  # SW_RESTORE
    else:
        usuario.ShowWindow(hwnd, 5)  # SW_SHOW
    usuario.keybd_event(0x12, 0, 0, 0)  # Alt abajo: habilita el cambio de foco
    usuario.keybd_event(0x12, 0, 0x2, 0)  # Alt arriba
    primer_plano = usuario.GetForegroundWindow()
    hilo_primer_plano = usuario.GetWindowThreadProcessId(primer_plano, None)
    hilo_propio = kernel.GetCurrentThreadId()
    unido = bool(hilo_primer_plano) and hilo_primer_plano != hilo_propio
    if unido:
        usuario.AttachThreadInput(hilo_propio, hilo_primer_plano, True)
    try:
        # «Siempre encima» y de vuelta: obliga a Windows a subirla por encima de las demás.
        usuario.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)
        usuario.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)
        usuario.BringWindowToTop(hwnd)
        usuario.SetForegroundWindow(hwnd)
    finally:
        if unido:
            usuario.AttachThreadInput(hilo_propio, hilo_primer_plano, False)


def _traer_al_frente(titulo: str, previas: set[int]) -> None:  # pragma: no cover - solo Windows
    """Pone al frente la ventana del Explorador que muestra la carpeta recién abierta.

    `previas` son las ventanas con ese título que ya existían antes de abrirla. Si el Explorador
    creó una ventana nueva, es esa; si ya había una con esa carpeta (la abrieron dos veces), Windows
    no crea otra: se enfoca la existente. Antes se tomaba la primera con ese título, que en la
    segunda apertura era la vieja y la nueva quedaba minimizada. Es un esfuerzo opcional.
    """
    for _ in range(40):  # hasta ~4 s esperando que el Explorador cree o muestre la ventana
        ventanas = _ventanas_del_explorador(titulo)
        nuevas = [v for v in ventanas if v not in previas]
        elegida = (nuevas or ventanas or [None])[0]
        if elegida is not None:
            _enfocar(elegida)
            return
        time.sleep(0.1)


def _abrir_en_explorador(ruta: Path) -> None:
    if sys.platform == "win32":
        previas = set(_ventanas_del_explorador(ruta.name))  # pragma: no cover
        os.startfile(ruta)
        threading.Thread(target=_traer_al_frente, args=(ruta.name, previas), daemon=True).start()
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(ruta)])
    else:
        subprocess.Popen(["xdg-open", str(ruta)])


def _exigir_habilitado(settings: Settings) -> None:
    if not settings.local_abrir_carpeta:
        raise ConflictError(
            "Abrir carpetas solo funciona si el backend corre en tu PC "
            "(LOCAL_ABRIR_CARPETA=true en su .env).",
            details={"motivo": "deshabilitado"},
        )


def partes_del_comprobante(comprobante: Comprobante) -> list[str]:
    """`año/MES/RUC/SERIE-NÚMERO` (mes en letras), como la arman el frontend y el extractor.

    La contraparte es el proveedor en una compra y el cliente (RUC o, si no tiene, su documento)
    en una venta.
    """
    fecha = comprobante.fecha_emision
    if comprobante.origen is OrigenComprobante.recibido:
        contraparte = comprobante.ruc_emisor
    else:
        contraparte = comprobante.ruc_receptor or (comprobante.receptor_doc or "").strip()
    return [
        _limpio(str(fecha.year) if fecha else "", "sin-fecha"),
        _limpio(carpeta_de_mes(fecha.month) if fecha else "", "sin-fecha"),
        _limpio(contraparte or "", "sin-ruc"),
        _limpio(documento_del_comprobante(comprobante), "sin-numero"),
    ]


async def restaurar_carpeta(
    *,
    uow: UnitOfWork,
    settings: Settings,
    comprobante: Comprobante,
    storage_provider: StorageProvider | None = None,
    mensaje_original: str | None = None,
) -> Path | None:
    """Deja en la PC la carpeta del comprobante: archivos del bucket y, en compras, `correo.txt`.

    Copia solo lo que falta (nunca pisa un archivo existente, ni un `correo.txt` hecho por el
    extractor, que además trae el cuerpo del correo). El `correo.txt` sale de lo que Supabase guarda
    del correo: es la constancia, junto a los archivos, de que la factura llegó por el buzón. Si se
    conoce la ruta del correo completo (`mensaje_original`, un `.msg`), se anota en el texto y se
    actualiza un `correo.txt` del portal que aún no la traía.
    Devuelve la carpeta, o `None` si no hay nada que dejar (sin archivos ni correo y sin carpeta).
    """
    es_compra = comprobante.origen is OrigenComprobante.recibido
    base = carpetas_base(settings, "recibido" if es_compra else "emitido")[0]
    destino = base.joinpath(*partes_del_comprobante(comprobante)).resolve()
    if not destino.is_relative_to(base):  # defensa en profundidad: las partes ya están limpias
        raise NotFoundError("La carpeta del comprobante no es válida")

    archivos = [
        a
        for a in await uow.archivos.list_for_comprobante(comprobante.id)
        if a.storage_key and a.tipo in (TipoArchivo.xml, TipoArchivo.pdf, TipoArchivo.cdr)
    ]
    texto_correo = (
        texto_correo_de_compra(comprobante, archivos, mensaje_original=mensaje_original)
        if es_compra
        else None
    )
    if not archivos and not destino.is_dir():
        return None

    proveedor = storage_provider or get_storage_provider(settings)
    copiados = 0
    for archivo in archivos:
        ruta = destino / _limpio(archivo.nombre, f"{archivo.tipo.value}")
        if ruta.exists():
            continue
        datos = await proveedor.get_object_bytes(archivo.storage_key or "")
        await asyncio.to_thread(_escribir, ruta, datos)
        copiados += 1
    ruta_correo = destino / NOMBRE_CORREO_TXT
    if texto_correo is not None and (
        not ruta_correo.exists()
        or (mensaje_original and correo_txt_del_portal_sin_msg(ruta_correo))
    ):
        await asyncio.to_thread(_escribir, ruta_correo, texto_correo.encode("utf-8"))
        copiados += 1
    logger.info("Carpeta local restaurada desde el bucket: %s (%d archivos)", destino, copiados)
    return destino


async def restaurar_y_abrir_carpeta(
    *,
    uow: UnitOfWork,
    settings: Settings,
    user: AuthenticatedUser,
    comprobante_id: UUID,
    storage_provider: StorageProvider | None = None,
) -> str:
    """Reconstruye la carpeta del comprobante desde el bucket (R2) y la abre.

    La carpeta local solo existe si el extractor del correo o «Guardar en mi PC» la crearon; un
    comprobante cuyo XML o PDF entró de otra forma (SIRE + PDF unido, otra PC, importación manual)
    tiene sus archivos en el bucket pero no en esta PC, y «Abrir carpeta» fallaba. Aquí se copian
    los que falten y se abre. La ruta sale del comprobante en la base de datos (con RLS), no de lo
    que mande el cliente.
    """
    _exigir_habilitado(settings)
    comprobante = await uow.comprobantes.get(comprobante_id)
    if comprobante is None:
        raise NotFoundError("El comprobante no existe o no pertenece a sus empresas")
    await require_membership(uow, user, comprobante.company_id)

    destino = await restaurar_carpeta(
        uow=uow, settings=settings, comprobante=comprobante, storage_provider=storage_provider
    )
    if destino is None:
        raise NotFoundError(
            "Este comprobante no tiene archivos guardados que copiar a la PC.",
            details={"motivo": "sin_archivos"},
        )
    _abrir_en_explorador(destino)
    return str(destino)


def correo_txt_del_portal_sin_msg(ruta: Path) -> bool:
    """¿Es un `correo.txt` armado por el portal (sin cuerpo) que aún no indica su `.msg`?

    El del extractor trae «Cuerpo:» y nunca se reemplaza: tiene más información.
    """
    try:
        texto = ruta.read_text(encoding="utf-8")
    except OSError:
        return False
    del_portal = "Buzón:     recepcionfacturas" in texto and "Cuerpo:" not in texto
    return del_portal and "(.msg)" not in texto


def _escribir(ruta: Path, datos: bytes) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    temporal = ruta.with_name(ruta.name + ".parcial")
    temporal.write_bytes(datos)
    temporal.replace(ruta)  # nunca queda un archivo a medio escribir con el nombre final


MAX_CORREO_TXT = 200_000  # bytes: el cuerpo de un correo largo no debe inflar la respuesta


def leer_correos_txt_locales(settings: Settings, comprobante: Comprobante) -> list[tuple[str, str]]:
    """Los `correo.txt` del extractor en la carpeta de la compra: `[(nombre, texto)]`.

    Una factura que llega en dos correos deja `correo.txt` y `correo (2).txt`: se leen todos, el
    primero antes. Son los más completos (traen el cuerpo del correo). Se leen solo si
    `LOCAL_ABRIR_CARPETA=true` (el backend está en la misma PC que la carpeta) y de una carpeta que
    ya existe dentro de la base de compras; en un servidor la lista sale vacía y se usa el armado
    con lo que guarda la base.
    """
    if not settings.local_abrir_carpeta or comprobante.origen is not OrigenComprobante.recibido:
        return []
    base = carpeta_proveedores(settings)
    for variante in _variantes_de_mes(partes_del_comprobante(comprobante)):
        carpeta = base.joinpath(*variante).resolve()
        if not carpeta.is_relative_to(base) or not carpeta.is_dir():
            continue
        correos: list[tuple[str, str]] = []
        for ruta in sorted(carpeta.glob("correo*.txt"), key=lambda r: (len(r.name), r.name)):
            try:
                with ruta.open("rb") as archivo:
                    texto = archivo.read(MAX_CORREO_TXT).decode("utf-8", errors="replace")
            except OSError:
                continue
            correos.append((ruta.name, texto))
        return correos
    return []


def _variantes_de_mes(partes: list[str]) -> list[list[str]]:
    """La ruta con el mes como se guarda hoy (`10. OCTUBRE`) y las formas de antes.

    Las carpetas guardadas antes siguen abriéndose: con el nombre solo (`OCTUBRE`) o con el
    número (`10`). Sea cual sea la forma que mande el navegador, se prueban las tres.
    """
    if len(partes) < 2:
        return [partes]
    return [[partes[0], mes, *partes[2:]] for mes in variantes_de_mes(partes[1])]


def abrir_carpeta(*, settings: Settings, partes: list[str], origen: str | None = None) -> str:
    """Abre en el explorador la carpeta `año/mes/RUC/documento` y devuelve su ruta completa.

    No consulta la base de datos (la apertura es inmediata): el frontend manda las partes que ya
    conoce y aquí se limpian de separadores y caracteres reservados, de modo que nunca puede salir
    de la carpeta base ni abrir algo que no exista.
    """
    _exigir_habilitado(settings)
    limpias = [_limpio(parte, "sin-dato") for parte in partes]
    ruta: Path | None = None
    for base in carpetas_base(settings, origen):
        for variante in _variantes_de_mes(limpias):
            candidata = base.joinpath(*variante).resolve()
            if not candidata.is_relative_to(base):  # defensa en profundidad: ya están limpios
                raise NotFoundError("La carpeta del comprobante no es válida")
            if candidata.is_dir():
                ruta = candidata
                break
        if ruta is not None:
            break
    if ruta is None:
        raise NotFoundError(
            "Esa carpeta aún no existe en la PC: usa «Guardar en mi PC» primero.",
            details={"ruta": str(carpetas_base(settings, origen)[0].joinpath(*limpias))},
        )
    _abrir_en_explorador(ruta)
    logger.info("Carpeta local abierta: %s", ruta)
    return str(ruta)
