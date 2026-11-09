"""Da de alta una empresa del grupo (otro RUC) y le da acceso a los usuarios de otra.

Cada empresa es independiente en el portal: sus comprobantes, su carpeta y sus usuarios no se
mezclan con los de las demás. Sirve para sumar razones sociales (Holding TDM S.A.C. y las demás):

    uv run python -m scripts.alta_empresa --ruc 20615204791 \
        --razon-social "HOLDING TDM S.A.C." --copiar-usuarios-de 20514831620 [--aplicar]

Sin `--aplicar` solo muestra lo que haría. Es idempotente: si la empresa ya existe no la duplica y
solo agrega los usuarios que falten (nunca cambia el rol de uno que ya tiene acceso).
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from typing import Any

import asyncpg

from app.core.config import get_settings


async def dar_de_alta(
    conexion: asyncpg.Connection,
    *,
    ruc: str,
    razon_social: str,
    nombre_comercial: str | None,
    direccion: str | None,
    copiar_usuarios_de: str | None,
    aplicar: bool,
) -> dict[str, Any]:
    existente = await conexion.fetchrow(
        "select id, razon_social from public.companies where ruc = $1", ruc
    )
    resumen: dict[str, Any] = {
        "empresa": "ya existía" if existente else "se creará",
        "usuarios_nuevos": [],
        "usuarios_ya_tenian": [],
    }
    async with conexion.transaction():
        if existente:
            empresa_id = existente["id"]
        elif aplicar:
            empresa_id = await conexion.fetchval(
                """
                insert into public.companies (ruc, razon_social, nombre_comercial, direccion)
                values ($1, $2, $3, $4) returning id
                """,
                ruc,
                razon_social,
                nombre_comercial or razon_social,
                direccion,
            )
            resumen["empresa"] = "creada"
        else:
            empresa_id = None

        if copiar_usuarios_de:
            origen = await conexion.fetch(
                """
                select uc.user_id, uc.rol, u.email
                  from public.user_companies uc
                  join public.companies c on c.id = uc.company_id
                  join auth.users u on u.id = uc.user_id
                 where c.ruc = $1 and uc.activo
                """,
                copiar_usuarios_de,
            )
            for fila in origen:
                ya = empresa_id is not None and await conexion.fetchval(
                    "select 1 from public.user_companies where user_id = $1 and company_id = $2",
                    fila["user_id"],
                    empresa_id,
                )
                if ya:
                    resumen["usuarios_ya_tenian"].append(fila["email"])
                    continue
                resumen["usuarios_nuevos"].append(f"{fila['email']} ({fila['rol']})")
                if aplicar and empresa_id is not None:
                    await conexion.execute(
                        "insert into public.user_companies (user_id, company_id, rol)"
                        " values ($1, $2, $3)",
                        fila["user_id"],
                        empresa_id,
                        fila["rol"],
                    )
    return resumen


async def _principal(args: argparse.Namespace) -> int:
    if not re.fullmatch(r"\d{11}", args.ruc):
        print("El RUC debe tener 11 dígitos", file=sys.stderr)
        return 2
    url = get_settings().database_url
    if url is None:
        print("Falta DATABASE_URL", file=sys.stderr)
        return 2
    conexion = await asyncpg.connect(url.get_secret_value(), statement_cache_size=0)
    try:
        resumen = await dar_de_alta(
            conexion,
            ruc=args.ruc,
            razon_social=args.razon_social,
            nombre_comercial=args.nombre_comercial,
            direccion=args.direccion,
            copiar_usuarios_de=args.copiar_usuarios_de,
            aplicar=args.aplicar,
        )
    finally:
        await conexion.close()
    print(("REAL" if args.aplicar else "SIMULACIÓN"), resumen)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--ruc", required=True)
    parser.add_argument("--razon-social", required=True)
    parser.add_argument("--nombre-comercial", default=None)
    parser.add_argument("--direccion", default=None)
    parser.add_argument(
        "--copiar-usuarios-de", default=None, help="RUC de la empresa cuyos usuarios acceden"
    )
    parser.add_argument("--aplicar", action="store_true", help="Escribe de verdad (si no, simula)")
    return asyncio.run(_principal(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
