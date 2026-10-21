"""Carpeta local de respaldo: `Descargas/AAAA/MM/SERIE-NÚMERO/RUC`.

El navegador guarda ahí el XML y el CDR (ver el frontend). Este módulo, cuando el backend corre
en la MISMA PC del usuario, pide al sistema operativo abrirla. Por seguridad nunca recibe una
ruta del cliente: solo nombres de carpeta que se limpian y se limitan a la carpeta base configurada
(`LOCAL_DESCARGAS_DIR`, por defecto `~/Downloads`).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger

logger = get_logger(__name__)

_NO_PERMITIDO = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def _limpio(texto: str, vacio: str) -> str:
    return _NO_PERMITIDO.sub("-", texto).strip(" .") or vacio


def carpeta_base(settings: Settings) -> Path:
    return (settings.local_descargas_dir or Path.home() / "Downloads").resolve()


def _traer_al_frente(titulo: str) -> None:  # pragma: no cover - solo Windows, con ventanas reales
    """Restaura y enfoca la ventana del Explorador cuyo título es `titulo`.

    Windows no deja que un proceso en segundo plano (el backend) pase una ventana al frente: la
    abre minimizada en la barra de tareas. Pulsar y soltar Alt antes de `SetForegroundWindow`
    habilita ese cambio de foco. Es un esfuerzo opcional: si no la encuentra, no hace nada.
    """
    import ctypes

    usuario = ctypes.windll.user32
    for _ in range(30):  # hasta ~3 s esperando que el Explorador cree la ventana
        hwnd = usuario.FindWindowW("CabinetWClass", titulo)
        if hwnd:
            usuario.keybd_event(0x12, 0, 0, 0)  # Alt abajo
            usuario.keybd_event(0x12, 0, 0x2, 0)  # Alt arriba
            usuario.ShowWindow(hwnd, 9)  # SW_RESTORE
            usuario.SetForegroundWindow(hwnd)
            return
        time.sleep(0.1)


def _abrir_en_explorador(ruta: Path) -> None:
    if sys.platform == "win32":
        os.startfile(ruta)
        threading.Thread(target=_traer_al_frente, args=(ruta.name,), daemon=True).start()
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(ruta)])
    else:
        subprocess.Popen(["xdg-open", str(ruta)])


def abrir_carpeta(*, settings: Settings, partes: list[str]) -> str:
    """Abre en el explorador la carpeta `año/mes/documento/RUC` y devuelve su ruta completa.

    No consulta la base de datos (la apertura es inmediata): el frontend manda las partes que ya
    conoce y aquí se limpian de separadores y caracteres reservados, de modo que nunca puede salir
    de la carpeta base ni abrir algo que no exista.
    """
    if not settings.local_abrir_carpeta:
        raise ConflictError(
            "Abrir carpetas solo funciona si el backend corre en tu PC "
            "(LOCAL_ABRIR_CARPETA=true en su .env).",
            details={"motivo": "deshabilitado"},
        )
    base = carpeta_base(settings)
    ruta = base.joinpath(*(_limpio(parte, "sin-dato") for parte in partes)).resolve()
    if not ruta.is_relative_to(base):  # defensa en profundidad: los nombres ya están limpios
        raise NotFoundError("La carpeta del comprobante no es válida")
    if not ruta.is_dir():
        raise NotFoundError(
            "Esa carpeta aún no existe en la PC: usa «Guardar en mi PC» primero.",
            details={"ruta": str(ruta)},
        )
    _abrir_en_explorador(ruta)
    logger.info("Carpeta local abierta: %s", ruta)
    return str(ruta)
