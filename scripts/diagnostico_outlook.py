"""Diagnóstico de SOLO LECTURA de un buzón en Outlook clásico de Windows.

Cuenta, por carpeta, cuántos correos hay, desde y hasta qué fecha, cuántos traen adjuntos, de
qué tipo y de qué tamaño. No guarda nada, no modifica ni mueve ningún correo y NO imprime
asuntos, remitentes ni nombres de archivo: solo totales. Sirve para decidir cuánto histórico hay
y cómo llegan los adjuntos antes de extraerlos.

Requisitos: Windows, Outlook CLÁSICO abierto con el buzón agregado (el «nuevo Outlook» no
admite automatización) y `pywin32`:

    uv run --with pywin32 python -m scripts.diagnostico_outlook --buzon recepcionfacturas
    uv run --with pywin32 python -m scripts.diagnostico_outlook --buzon recepcionfacturas \
        --carpeta "Bandeja de entrada"

`--buzon` es un trozo del nombre del buzón tal como lo muestra Outlook (sin distinguir mayúsculas).
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

OL_MAIL = 43  # OlObjectClass.olMail
OL_BY_VALUE = 1  # OlAttachmentType.olByValue: archivo real (no enlace ni OLE incrustado)
EXTENSIONES_UTILES = {"xml", "pdf", "zip"}
EXTENSIONES_DE_FIRMA = {"png", "jpg", "jpeg", "gif", "bmp", "ico", "emf", "wmf", "svg"}


@dataclass
class Resumen:
    nombre: str
    correos: int = 0
    con_adjuntos: int = 0
    con_utiles: int = 0
    primero: datetime | None = None
    ultimo: datetime | None = None
    por_extension: Counter[str] = field(default_factory=Counter)
    adjuntos_por_correo: Counter[int] = field(default_factory=Counter)
    combinaciones: Counter[str] = field(default_factory=Counter)
    bytes_utiles: int = 0
    sin_fecha: int = 0
    errores: int = 0


def _extension(nombre: str) -> str:
    return nombre.rsplit(".", 1)[-1].lower() if "." in nombre else "(sin extensión)"


def _fecha(valor: Any) -> datetime | None:
    try:
        return datetime(valor.year, valor.month, valor.day)
    except Exception:
        return None


def resumir_carpeta(carpeta: Any) -> Resumen:
    """Recorre los correos de la carpeta (sin tocarlos) y acumula los totales."""
    resumen = Resumen(nombre=str(carpeta.Name))
    items = carpeta.Items
    total = items.Count
    for posicion in range(1, total + 1):
        if posicion % 500 == 0:
            print(f"    … {posicion}/{total}", file=sys.stderr, flush=True)
        try:
            correo = items.Item(posicion)
            if correo.Class != OL_MAIL:
                continue
            resumen.correos += 1
            fecha = _fecha(correo.ReceivedTime)
            if fecha is None:
                resumen.sin_fecha += 1
            else:
                resumen.primero = min(resumen.primero or fecha, fecha)
                resumen.ultimo = max(resumen.ultimo or fecha, fecha)
            adjuntos = correo.Attachments
            if adjuntos.Count == 0:
                continue
            resumen.con_adjuntos += 1
            extensiones: list[str] = []
            for indice in range(1, adjuntos.Count + 1):
                adjunto = adjuntos.Item(indice)
                if adjunto.Type != OL_BY_VALUE:
                    continue
                extension = _extension(str(adjunto.FileName))
                if extension in EXTENSIONES_DE_FIRMA:
                    continue  # logos y firmas
                resumen.por_extension[extension] += 1
                extensiones.append(extension)
                if extension in EXTENSIONES_UTILES:
                    resumen.bytes_utiles += int(adjunto.Size)
            if any(e in EXTENSIONES_UTILES for e in extensiones):
                resumen.con_utiles += 1
                resumen.adjuntos_por_correo[len(extensiones)] += 1
                resumen.combinaciones[",".join(sorted(extensiones))] += 1
        except Exception:
            resumen.errores += 1
    return resumen


def _carpetas(raiz: Any, solo: str | None, ruta: str = "") -> list[Any]:
    """Todas las carpetas del buzón (o solo la indicada), con sus subcarpetas."""
    encontradas: list[Any] = []
    for carpeta in raiz.Folders:
        nombre = f"{ruta}/{carpeta.Name}" if ruta else str(carpeta.Name)
        if solo is None or str(carpeta.Name).lower() == solo.lower():
            encontradas.append((nombre, carpeta))
        encontradas.extend(_carpetas(carpeta, solo, nombre))
    return encontradas


def imprimir(ruta: str, r: Resumen) -> None:
    if r.correos == 0:
        print(f"\n{ruta}: vacía")
        return
    rango = (
        f"{r.primero:%Y-%m-%d} → {r.ultimo:%Y-%m-%d}" if r.primero and r.ultimo else "sin fechas"
    )
    print(f"\n{ruta}")
    print(f"  correos: {r.correos} | rango: {rango} | con adjuntos: {r.con_adjuntos}")
    print(
        f"  con XML/PDF/ZIP: {r.con_utiles} | peso de esos adjuntos: {r.bytes_utiles / 1e6:.1f} MB"
    )
    if r.por_extension:
        print("  extensiones:", dict(r.por_extension.most_common(8)))
    if r.adjuntos_por_correo:
        print("  adjuntos útiles por correo:", dict(sorted(r.adjuntos_por_correo.items())))
        print("  combinaciones más comunes:", dict(r.combinaciones.most_common(6)))
    if r.sin_fecha or r.errores:
        print(f"  sin fecha: {r.sin_fecha} | ilegibles: {r.errores}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Diagnóstico de solo lectura de un buzón de Outlook"
    )
    parser.add_argument("--buzon", required=True, help="Parte del nombre del buzón en Outlook")
    parser.add_argument("--carpeta", default=None, help="Solo esta carpeta (nombre exacto)")
    args = parser.parse_args(argv)

    try:
        import win32com.client  # type: ignore[import-not-found]
    except ImportError:
        print(
            "Falta pywin32: ejecute con "
            "`uv run --with pywin32 python -m scripts.diagnostico_outlook`"
        )
        return 2

    espacio = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
    buzones = [b for b in espacio.Folders if args.buzon.lower() in str(b.Name).lower()]
    if len(buzones) != 1:
        nombres = [str(b.Name) for b in espacio.Folders]
        print(f"Se esperaba 1 buzón con «{args.buzon}» y hay {len(buzones)}. Buzones: {nombres}")
        return 2

    buzon = buzones[0]
    print(f"Buzón: {buzon.Name}")
    total = Resumen(nombre="TOTAL")
    for ruta, carpeta in _carpetas(buzon, args.carpeta):
        print(f"Leyendo {ruta}…", file=sys.stderr, flush=True)
        resumen = resumir_carpeta(carpeta)
        imprimir(ruta, resumen)
        total.correos += resumen.correos
        total.con_utiles += resumen.con_utiles
        total.bytes_utiles += resumen.bytes_utiles
        total.por_extension.update(resumen.por_extension)
        total.adjuntos_por_correo.update(resumen.adjuntos_por_correo)
        if resumen.primero:
            total.primero = min(total.primero or resumen.primero, resumen.primero)
        if resumen.ultimo:
            total.ultimo = max(total.ultimo or resumen.ultimo, resumen.ultimo)
    print("\n=== TOTAL ===")
    imprimir("todo el buzón", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
