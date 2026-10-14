"""Prueba de humo end-to-end contra Supabase (y opcionalmente SUNAT).

Recorre los endpoints reales: `/health`, `/companies`, listado, `/sunat/status`,
`refresh-cdr` y la importación de un XML UBL. La importación se limpia al final.

IMPORTANTE: inyecta un verificador de JWT falso para el usuario indicado porque
no emitimos tokens de Supabase aquí. Es una herramienta SOLO para pruebas
locales; no forma parte de la aplicación ni debe usarse en producción.

    uv run python -m scripts.e2e_supabase --ruc 20514831620 --user-id <uuid>
    uv run python -m scripts.e2e_supabase --ruc 20514831620 --user-id <uuid> --skip-sunat

Sale con código 0 si todas las comprobaciones pasan, 1 si alguna falla.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from uuid import UUID

import asyncpg
from fastapi.testclient import TestClient
from scripts.aplicar_migraciones import normalizar_dsn

from app.core.config import Settings
from app.core.security import AuthenticatedUser
from app.main import create_app
from app.secrets.env import EnvSecretsProvider

RAIZ = Path(__file__).resolve().parent.parent
CORRELATIVO_PRUEBA = 9900


def cargar_env(ruta: Path) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if not ruta.exists():
        return mapping
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if linea and not linea.startswith("#") and "=" in linea:
            clave, valor = linea.split("=", 1)
            mapping[clave.strip()] = valor.strip()
    return mapping


class _VerificadorFalso:
    """Devuelve siempre el usuario indicado (sin validar el token)."""

    def __init__(self, user_id: UUID, email: str) -> None:
        self._user_id = user_id
        self._email = email

    async def verify(self, token: str) -> AuthenticatedUser:
        return AuthenticatedUser(
            id=self._user_id, email=self._email, role="authenticated", claims={}
        )


def _xml_de_prueba(ruc: str) -> bytes:
    xml = (RAIZ / "tests" / "fixtures" / "ubl" / "factura.xml").read_bytes()
    return (
        xml.replace(b"20123456789", ruc.encode())
        .replace(b"F001-00000001", f"F001-{CORRELATIVO_PRUEBA:08d}".encode())
    )


async def _limpiar(ruc: str) -> None:
    settings = Settings(_env_file=".env")
    if not settings.database_configured or settings.database_url is None:
        return
    dsn, ssl = normalizar_dsn(settings.database_url.get_secret_value())
    conexion = await asyncpg.connect(dsn, ssl=ssl)
    try:
        await conexion.execute(
            "delete from public.comprobantes "
            "where ruc_emisor=$1 and serie='F001' and correlativo=$2",
            ruc,
            CORRELATIVO_PRUEBA,
        )
    finally:
        await conexion.close()


def _chequear(nombre: str, condicion: bool, detalle: str = "") -> bool:
    marca = "OK " if condicion else "FALLA"
    print(f"[{marca}] {nombre}{(' -> ' + detalle) if detalle else ''}")
    return condicion


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prueba e2e contra Supabase")
    parser.add_argument("--ruc", required=True)
    parser.add_argument("--user-id", required=True, help="UUID del usuario en Supabase Auth")
    parser.add_argument("--email", default="demo@empresa.pe")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--skip-sunat", action="store_true")
    args = parser.parse_args(argv)

    mapping = cargar_env(Path(args.env_file))
    settings = Settings(_env_file=args.env_file)
    app = create_app(settings)
    app.state.jwt_verifier = _VerificadorFalso(UUID(args.user_id), args.email)
    app.state.secrets_provider = EnvSecretsProvider(mapping)
    auth = {"Authorization": "Bearer token-local"}

    resultados: list[bool] = []
    with TestClient(app) as client:
        r = client.get("/health")
        resultados.append(_chequear("GET /health", r.status_code == 200))
        if not settings.database_configured:
            print("DATABASE_URL no configurada: se omite el resto.")
            return 1

        r = client.get("/api/v1/companies", headers=auth)
        empresas = r.json().get("items", []) if r.status_code == 200 else []
        resultados.append(
            _chequear("GET /companies", r.status_code == 200, f"{len(empresas)} empresa(s)")
        )
        resultados.append(
            _chequear("empresa esperada presente", any(e["ruc"] == args.ruc for e in empresas))
        )

        r = client.get(f"/api/v1/companies/{args.ruc}/comprobantes", headers=auth)
        items = r.json().get("items", []) if r.status_code == 200 else []
        resultados.append(
            _chequear("GET comprobantes", r.status_code == 200, f"{len(items)} item(s)")
        )

        if not args.skip_sunat:
            r = client.get(f"/api/v1/companies/{args.ruc}/sunat/status", headers=auth)
            detalle = r.json().get("estado") if r.status_code == 200 else str(r.status_code)
            resultados.append(_chequear("GET sunat/status", r.status_code == 200, str(detalle)))

            if items:
                r = client.post(f"/api/v1/comprobantes/{items[0]['id']}/refresh-cdr", headers=auth)
                nuevo = r.json().get("estado_nuevo") if r.status_code == 200 else r.text[:120]
                resultados.append(_chequear("POST refresh-cdr", r.status_code == 200, str(nuevo)))

        r = client.post(
            f"/api/v1/companies/{args.ruc}/comprobantes/import",
            headers=auth,
            files={"archivo": ("e2e.xml", _xml_de_prueba(args.ruc), "application/xml")},
        )
        resultados.append(_chequear("POST import UBL", r.status_code == 201, str(r.status_code)))
        if r.status_code == 201:
            r2 = client.post(
                f"/api/v1/companies/{args.ruc}/comprobantes/import",
                headers=auth,
                files={"archivo": ("e2e.xml", _xml_de_prueba(args.ruc), "application/xml")},
            )
            resultados.append(_chequear("import duplicado -> 409", r2.status_code == 409))

    asyncio.run(_limpiar(args.ruc))
    print("Limpieza del comprobante de prueba hecha.")

    ok = all(resultados)
    print("\nRESULTADO:", "TODO OK" if ok else "HAY FALLOS")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
