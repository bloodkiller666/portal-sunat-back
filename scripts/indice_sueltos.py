"""Índice con enlaces a los PDF sueltos (`_sin_clasificar`): los que no se unieron a una compra.

Un PDF suelto llegó en un correo sin ninguna factura con XML, o su texto no identifica una compra
del portal. Este script no mueve ni borra nada: arma `_sin_clasificar/INDICE.html`, con un
enlace a la carpeta de cada correo y a cada PDF, agrupados por el motivo por el que siguen sueltos:

* **ya está en el portal**: el mismo archivo ya se subió a otra compra (sobra aquí);
* **escaneo**: el PDF no tiene texto, no se puede leer sin OCR;
* **sin serie-número**: orden de compra, cotización u otro documento sin factura citada;
* **cita otro documento**: nombra un serie-número que no es una compra del portal (guía de remisión,
  factura de otra empresa del grupo…).

Solo lectura sobre la base (consulta los hashes ya archivados). Uso:

    uv run python -m scripts.indice_sueltos
    uv run python -m scripts.indice_sueltos --destino "C:\\ruta\\FacturasProveedores" --sin-base
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import asyncpg
from scripts.aplicar_migraciones import normalizar_dsn

from app.core.config import Settings
from app.services.pdf_identificar import candidatos_de_pdf, texto_del_pdf

MOTIVOS = {
    "ya_en_portal": "Ya está en el portal (sobra aquí)",
    "escaneo": "Escaneo: sin texto, haría falta OCR",
    "sin_serie": "Sin serie-número (orden de compra, cotización…)",
    "cita_otro": "Cita otro documento (guía, factura de otra empresa…)",
}
_ORDEN = list(MOTIVOS)


@dataclass(frozen=True)
class Suelto:
    pdf: Path
    motivo: str
    correo: Path  # carpeta del correo


def clasificar(contenido: bytes, archivados: set[str]) -> str:
    """El motivo por el que un PDF sigue suelto."""
    if hashlib.sha256(contenido).hexdigest() in archivados:
        return "ya_en_portal"
    if len(texto_del_pdf(contenido).strip()) < 30:
        return "escaneo"
    _rucs, series = candidatos_de_pdf(contenido)
    return "cita_otro" if series else "sin_serie"


def _datos_del_correo(carpeta: Path) -> tuple[str, str, str]:
    """(asunto, remitente, recibido) leídos del `correo.txt` de la carpeta."""
    ruta = carpeta / "correo.txt"
    datos = {"Asunto": "", "De": "", "Recibido": ""}
    if ruta.is_file():
        for linea in ruta.read_text(encoding="utf-8", errors="replace").splitlines()[:6]:
            clave, _, valor = linea.partition(":")
            if clave.strip() in datos:
                datos[clave.strip()] = valor.strip()
    return datos["Asunto"], datos["De"], datos["Recibido"]


def _enlace(ruta: Path, texto: str) -> str:
    return f'<a href="{html.escape(ruta.as_uri())}">{html.escape(texto)}</a>'


def construir_html(sueltos: list[Suelto], base: Path) -> str:
    """La página del índice (sin dependencias: se abre con doble clic)."""
    por_motivo: dict[str, dict[Path, list[Suelto]]] = {m: defaultdict(list) for m in MOTIVOS}
    for s in sueltos:
        por_motivo[s.motivo][s.correo].append(s)
    partes = [
        "<!doctype html><html lang='es'><head><meta charset='utf-8'>",
        "<title>PDF sueltos</title><style>",
        "body{font:14px system-ui,sans-serif;margin:24px auto;max-width:1100px;color:#1d2939}",
        "h1{font-size:22px}h2{margin-top:32px;font-size:17px;border-bottom:1px solid #d0d5dd}",
        ".correo{margin:12px 0;padding:8px 12px;background:#f9fafb;border-radius:8px}",
        ".meta{color:#667085;font-size:12.5px}.ruta{font-family:monospace;font-size:12px}",
        "ul{margin:4px 0 0 18px;padding:0}a{color:#1570ef}",
        "</style></head><body>",
        f"<h1>PDF sueltos: {len(sueltos)} archivos en "
        f"{len({s.correo for s in sueltos})} correos</h1>",
        f"<p class='meta'>Carpeta base: <span class='ruta'>{_enlace(base, str(base))}</span></p>",
        "<ul>",
    ]
    for motivo in _ORDEN:
        n = sum(len(v) for v in por_motivo[motivo].values())
        partes.append(f"<li><a href='#{motivo}'>{html.escape(MOTIVOS[motivo])}</a>: {n}</li>")
    partes.append("</ul>")
    for motivo in _ORDEN:
        correos = por_motivo[motivo]
        cantidad = sum(map(len, correos.values()))
        partes.append(f"<h2 id='{motivo}'>{html.escape(MOTIVOS[motivo])} ({cantidad})</h2>")
        for carpeta in sorted(correos, key=lambda c: str(c)):
            asunto, remitente, recibido = _datos_del_correo(carpeta)
            partes.append("<div class='correo'>")
            partes.append(f"<strong>{html.escape(asunto or '(sin asunto)')}</strong>")
            quien = f"{html.escape(remitente)} · {html.escape(recibido)}"
            partes.append(f"<div class='meta'>{quien}</div>")
            relativa = carpeta.relative_to(base).as_posix()
            partes.append(f"<div>Carpeta: {_enlace(carpeta, relativa)}</div><ul>")
            for s in sorted(correos[carpeta], key=lambda x: x.pdf.name):
                partes.append(f"<li>{_enlace(s.pdf, s.pdf.name)}</li>")
            partes.append("</ul></div>")
    partes.append("</body></html>")
    return "\n".join(partes)


async def _hashes_archivados(settings: Settings) -> set[str]:
    if not settings.database_configured or settings.database_url is None:
        return set()
    dsn, ssl = normalizar_dsn(settings.database_url.get_secret_value())
    conexion = await asyncpg.connect(dsn, ssl=ssl)
    try:
        filas = await conexion.fetch(
            "select hash_sha256 from archivos where hash_sha256 is not null"
        )
        return {f[0] for f in filas}
    finally:
        await conexion.close()


def recolectar(base: Path, archivados: set[str]) -> list[Suelto]:
    sueltos = []
    for pdf in sorted(base.rglob("*.pdf")):
        correo = pdf.parent
        sueltos.append(Suelto(pdf, clasificar(pdf.read_bytes(), archivados), correo))
    return sueltos


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Índice de los PDF sueltos con enlaces")
    parser.add_argument("--destino", default=None, help="Carpeta de compras (por defecto, el .env)")
    parser.add_argument("--sin-base", action="store_true", help="No consulta Supabase")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args(argv)

    settings = Settings(_env_file=args.env_file)
    destino = Path(args.destino) if args.destino else settings.local_proveedores_dir
    if destino is None:
        destino = Path.home() / "Downloads" / "FacturasProveedores"
    base = destino / "_sin_clasificar"
    if not base.is_dir():
        print(f"No existe {base}")
        return 1
    archivados = set() if args.sin_base else asyncio.run(_hashes_archivados(settings))
    sueltos = recolectar(base, archivados)
    salida = base / "INDICE.html"
    salida.write_text(construir_html(sueltos, base), encoding="utf-8")
    conteo = {m: sum(1 for s in sueltos if s.motivo == m) for m in MOTIVOS}
    print(f"{len(sueltos)} PDF sueltos en {len({s.correo for s in sueltos})} correos -> {salida}")
    for m, n in conteo.items():
        print(f"  {n:4d}  {MOTIVOS[m]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
