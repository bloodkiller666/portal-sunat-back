"""Aplica las migraciones de `supabase/migrations/` en orden.

Pensado para cuando no tienes el CLI de Supabase a mano:

    uv run python -m scripts.aplicar_migraciones            # usa DATABASE_URL del .env
    uv run python -m scripts.aplicar_migraciones --dsn "postgresql://..."

Normaliza la DSN de Supabase (`sslmode=require` no lo entiende asyncpg) y aplica
cada archivo `.sql` en orden alfabético. Detente en el primer error.

Ojo: las migraciones NO son idempotentes (`create type ...`); se aplican una vez
sobre una base vacía. Para bases ya inicializadas, no las vuelvas a correr.
"""

from __future__ import annotations

import argparse
import asyncio
import ssl as ssl_module
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg

from app.core.config import Settings
from app.core.redaction import redact

MIGRACIONES = Path(__file__).resolve().parent.parent / "supabase" / "migrations"


def normalizar_dsn(dsn: str) -> tuple[str, object]:
    """Quita `sslmode` (asyncpg no lo acepta) y decide el `ssl`.

    Sin `sslmode` se devuelve `None` (modo *prefer* de asyncpg), que es el mismo
    comportamiento del pool de la aplicación. Con `verify-ca`/`verify-full` se
    exige verificación (necesita el CA de Supabase, ver `--ssl-ca`).
    """
    partes = urlsplit(dsn)
    parametros = dict(parse_qsl(partes.query))
    sslmode = parametros.pop("sslmode", None)
    limpio = urlunsplit(
        (partes.scheme, partes.netloc, partes.path, urlencode(parametros), partes.fragment)
    )
    host = (partes.hostname or "").lower()
    if host in {"localhost", "127.0.0.1"} or sslmode == "disable":
        ssl: object = False
    elif sslmode in {"verify-ca", "verify-full"}:
        ssl = True
    elif sslmode == "require":
        ssl = "require"
    else:
        ssl = None
    return limpio, ssl


def contexto_ssl(ca: str | None) -> ssl_module.SSLContext | None:
    """Contexto con el CA de Supabase (`prod-ca-2021.crt`) si se indica."""
    if not ca:
        return None
    return ssl_module.create_default_context(cafile=ca)


async def aplicar(dsn: str, *, ssl_ca: str | None = None, desde: str | None = None) -> int:
    dsn_limpio, ssl = normalizar_dsn(dsn)
    contexto = contexto_ssl(ssl_ca)
    if contexto is not None:
        ssl = contexto
    archivos = sorted(MIGRACIONES.glob("*.sql"))
    if desde:
        archivos = [a for a in archivos if a.name >= desde]
    if not archivos:
        print("No hay migraciones que aplicar en", MIGRACIONES)
        return 1

    conexion = await asyncpg.connect(dsn_limpio, ssl=ssl)
    try:
        for archivo in archivos:
            print(f"aplicando {archivo.name} ...", flush=True)
            sql = archivo.read_text(encoding="utf-8")
            try:
                await conexion.execute(sql)
            except Exception as exc:
                print(f"FALLO en {archivo.name}: {type(exc).__name__}: {redact(str(exc))[:400]}")
                return 1
            print(f"  ok {archivo.name}")
    finally:
        await conexion.close()
    print("Migraciones aplicadas.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aplicar migraciones de Supabase")
    parser.add_argument("--dsn", help="DATABASE_URL (por defecto, la del .env)")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--ssl-ca",
        help="Ruta al prod-ca-2021.crt de Supabase para verificar el certificado",
    )
    parser.add_argument(
        "--desde",
        help="Aplica solo desde este archivo (p. ej. 0004); útil si ya aplicaste los anteriores",
    )
    args = parser.parse_args(argv)

    if args.dsn:
        dsn = args.dsn
    else:
        settings = Settings(_env_file=args.env_file)
        if not settings.database_configured or settings.database_url is None:
            print("ERROR: DATABASE_URL no está definida en el .env")
            return 1
        dsn = settings.database_url.get_secret_value()

    return asyncio.run(aplicar(dsn, ssl_ca=args.ssl_ca, desde=args.desde))


if __name__ == "__main__":
    sys.exit(main())
