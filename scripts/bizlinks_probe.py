"""Diagnóstico del respaldo XML/CDR en el portal anónimo de Bizlinks.

Consulta **un solo** comprobante emitido de la base de datos (Supabase) en el portal
público de Bizlinks y descarga su XML firmado y su CDR, sin escribir nada: ni en la base
ni en R2. Sirve para comprobar que el flujo del portal sigue vigente antes de habilitar
`BIZLINKS_CANAL=portal` en la API.

    uv run python -m scripts.bizlinks_probe                 # primer emitido con fecha y monto
    uv run python -m scripts.bizlinks_probe --serie F001 --numero 123

Nunca imprime credenciales. Los archivos, si se piden con `--guardar`, se escriben en
`var/bizlinks-probe/` (ignorado por git) para revisarlos con un editor o un antivirus.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import sys
import zipfile
from pathlib import Path
from typing import Any

import asyncpg
import httpx
from scripts.sunat_probe import cargar_env

from app.core.config import Settings
from app.core.errors import AppError, CredentialsNotConfiguredError
from app.secrets.base import CompanyCredentials
from app.secrets.env import EnvSecretsProvider
from app.sunat.bizlinks_portal import BizlinksPortalClient
from app.sunat.cdr import parse_cdr_zip
from app.sunat.ubl import parse_comprobante_ubl

SALIDA = Path("var/bizlinks-probe")

_CONSULTA = """
select id, tipo_documento, serie, correlativo, fecha_emision, monto_total, estado,
       razon_social_receptor, sunat_ambiente, fuente
from public.comprobantes
where company_id = (select id from public.companies where ruc = $1)
  and origen = 'emitido'
  and fecha_emision is not null
  and monto_total is not null
  and ($2::text is null or serie = $2)
  and ($3::int is null or correlativo = $3)
order by fecha_emision desc, correlativo desc
limit 1
"""


def _resumen_zip(contenido: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(contenido)) as archivo:
            return ", ".join(
                f"{info.filename} ({info.file_size} B)" for info in archivo.infolist()
            )
    except zipfile.BadZipFile:
        return "no es un ZIP"


async def _comprobante(dsn: str, ruc: str, serie: str | None, numero: int | None) -> Any:
    conexion = await asyncpg.connect(dsn)
    try:
        return await conexion.fetchrow(_CONSULTA, ruc, serie, numero)
    finally:
        await conexion.close()


def _credenciales(entorno: Path, ruc: str) -> CompanyCredentials:
    try:
        return EnvSecretsProvider(cargar_env(entorno)).get_company_credentials(ruc)
    except CredentialsNotConfiguredError as exc:
        raise SystemExit(f"ERROR: {exc.message}") from exc


async def ejecutar(args: argparse.Namespace) -> int:
    settings = Settings(_env_file=args.env_file)
    if not settings.database_configured:
        raise SystemExit("ERROR: falta DATABASE_URL en el entorno/.env")
    cred = _credenciales(Path(args.env_file), args.ruc)

    dsn = settings.database_url
    assert dsn is not None
    fila = await _comprobante(dsn.get_secret_value(), args.ruc, args.serie, args.numero)
    if fila is None:
        print("No hay comprobantes emitidos con fecha y monto para consultar.")
        return 1

    print(
        f"Comprobante: {fila['tipo_documento']} {fila['serie']}-{fila['correlativo']:08d}"
        f" | {fila['fecha_emision']} | S/ {fila['monto_total']} | {fila['estado']}"
        f" | ambiente {fila['sunat_ambiente']} | fuente {fila['fuente']}"
    )
    print(f"Cliente: {fila['razon_social_receptor'] or '(sin nombre)'}")

    cliente = BizlinksPortalClient(cred, settings)
    try:
        respaldos = await asyncio.to_thread(
            cliente.obtener_respaldos,
            tipo_documento=fila["tipo_documento"],
            serie=fila["serie"],
            correlativo=fila["correlativo"],
            fecha_emision=fila["fecha_emision"],
            monto_total=fila["monto_total"],
        )
    except AppError as exc:
        print(f"\nERROR ({exc.code}) HTTP {cliente.ultimo_http_status}: {exc.message}")
        if exc.details:
            print("detalles:", exc.details)
        return 1

    print(f"\nencontrado: {respaldos.encontrado}")
    for aviso in respaldos.avisos:
        print("aviso:", aviso)
    if not respaldos.encontrado:
        return 1

    if respaldos.xml:
        dato = parse_comprobante_ubl(respaldos.xml, ruc_esperado=args.ruc)
        print(
            f"XML: {len(respaldos.xml)} B | sha256 {hashlib.sha256(respaldos.xml).hexdigest()[:16]}"
            f" | {dato.tipo_documento} {dato.serie}-{dato.correlativo:08d}"
            f" | total {dato.monto_total} | {len(dato.items)} ítems"
        )
    if respaldos.cdr:
        print(f"CDR: {len(respaldos.cdr)} B | {_resumen_zip(respaldos.cdr)}")
        try:
            cdr = parse_cdr_zip(respaldos.cdr)
            print(f"CDR interpretado: {cdr.codigo} — {cdr.descripcion}")
        except AppError as exc:
            print(f"CDR no interpretable: {exc.message}")

    if args.guardar:
        destino = await asyncio.to_thread(
            _guardar, f"{fila['serie']}-{fila['correlativo']:08d}", respaldos
        )
        print(f"\nGuardado en {destino}")
    return 0 if respaldos.xml or respaldos.cdr else 1


def _guardar(base: str, respaldos: Any) -> Path:
    """Escribe los archivos descargados (en un hilo: la escritura es bloqueante)."""
    SALIDA.mkdir(parents=True, exist_ok=True)
    if respaldos.xml:
        (SALIDA / f"{base}.xml").write_bytes(respaldos.xml)
    if respaldos.cdr:
        (SALIDA / f"{base}-CDR.zip").write_bytes(respaldos.cdr)
    return SALIDA.resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prueba del portal anónimo de Bizlinks")
    parser.add_argument("--ruc", default="20514831620", help="RUC de la empresa")
    parser.add_argument("--serie", default=None, help="Serie del comprobante (opcional)")
    parser.add_argument("--numero", type=int, default=None, help="Correlativo (opcional)")
    parser.add_argument("--env-file", default=".env", help="Ruta del archivo .env")
    parser.add_argument(
        "--guardar", action="store_true", help="Escribe los archivos en var/bizlinks-probe/"
    )
    args = parser.parse_args(argv)
    try:
        return asyncio.run(ejecutar(args))
    except httpx.TransportError as exc:
        print(f"ERROR de red: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
