"""Diagnóstico SIRE: ¿la empresa está acogida y puede usar la API de ventas (RVIE)?

Sigue el manual oficial v31 del API SIRE:

1. Token (§5.1): POST `clientessol/{client_id}/oauth2/token/` con
   `grant_type=password`, `scope=https://api-sire.sunat.gob.pe`, client_id,
   client_secret, username = RUC + usuario SOL y la clave SOL.
2. Periodos RVIE (§5.2): GET de los periodos habilitados. Es solo lectura y
   no genera tickets; si responde 200 la empresa tiene el libro habilitado.
3. Descargar propuesta (§5.18): GET `exportapropuesta` del periodo. Solo genera
   un ticket de descarga (no modifica el registro). El manual no define un
   código de «no incorporado», así que se imprime el cod/msg tal como llega.

Ejemplo:

    uv run python -m scripts.sire_probe --ruc 20514831620 --periodo 202609

Nunca imprime client_secret, clave SOL ni el token: todo texto pasa por `redact`.
Sale con 0 si la empresa quedó confirmada como acogida, 1 si no o no se pudo saber.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from scripts.sunat_probe import cargar_env

from app.core.config import Settings
from app.core.errors import CredentialsNotConfiguredError
from app.core.redaction import redact, register_secret_values
from app.secrets.base import CompanyCredentials
from app.secrets.env import EnvSecretsProvider
from app.sunat.endpoints import (
    SIRE_EXPORTAR_PROPUESTA_URL,
    SIRE_PERIODOS_RVIE_URL,
    SIRE_SCOPE,
    SIRE_TOKEN_URL,
)

TIMEOUT = httpx.Timeout(30.0)


def _periodo_por_defecto() -> str:
    """Mes anterior al actual (el periodo más reciente que ya cerró)."""
    hoy = date.today()
    anio, mes = (hoy.year, hoy.month - 1) if hoy.month > 1 else (hoy.year - 1, 12)
    return f"{anio}{mes:02d}"


def _cuerpo(respuesta: httpx.Response) -> Any:
    try:
        return respuesta.json()
    except ValueError:
        return redact(respuesta.text[:400])


def _mostrar(titulo: str, respuesta: httpx.Response) -> None:
    print(f"\n===== {titulo} =====")
    print("http_status:", respuesta.status_code)
    cuerpo = _cuerpo(respuesta)
    texto = redact(str(cuerpo))
    print("respuesta:", texto[:1200])


def pedir_token(cliente: httpx.Client, cred: CompanyCredentials) -> str | None:
    assert cred.client_id and cred.client_secret
    respuesta = cliente.post(
        SIRE_TOKEN_URL.format(client_id=cred.client_id),
        data={
            "grant_type": "password",
            "scope": SIRE_SCOPE,
            "client_id": cred.client_id,
            "client_secret": cred.client_secret.get_secret_value(),
            "username": cred.sunat_username,
            "password": cred.sol_password.get_secret_value(),
        },
    )
    print("\n===== token (api-seguridad) =====")
    print("http_status:", respuesta.status_code)
    cuerpo = _cuerpo(respuesta)
    if respuesta.status_code == 200 and isinstance(cuerpo, dict) and cuerpo.get("access_token"):
        token = str(cuerpo["access_token"])
        register_secret_values(token)
        print("token obtenido: sí | expira en (s):", cuerpo.get("expires_in"))
        return token
    print("token obtenido: no |", redact(str(cuerpo))[:600])
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnóstico SIRE (RVIE)")
    parser.add_argument("--ruc", required=True, help="RUC de la empresa (11 dígitos)")
    parser.add_argument(
        "--periodo",
        default=_periodo_por_defecto(),
        help="Periodo AAAAMM (por defecto: mes anterior)",
    )
    parser.add_argument("--env-file", default=".env", help="Ruta del archivo .env")
    args = parser.parse_args(argv)

    Settings(_env_file=args.env_file)  # valida la configuración como lo hace la API
    try:
        cred = EnvSecretsProvider(cargar_env(Path(args.env_file))).get_company_credentials(
            args.ruc
        )
    except CredentialsNotConfiguredError as exc:
        print("ERROR:", exc.message)
        return 1
    if not cred.client_id or cred.client_secret is None:
        print("ERROR: faltan SUNAT_CRED_<RUC>_CLIENT_ID / CLIENT_SECRET (Credenciales API SUNAT)")
        return 1
    register_secret_values(cred.client_secret, cred.sol_password)

    print(f"RUC: {cred.ruc} | periodo: {args.periodo}")
    with httpx.Client(timeout=TIMEOUT) as cliente:
        token = pedir_token(cliente, cred)
        if token is None:
            print("\nVEREDICTO: no se pudo autenticar (revise credenciales de API y SOL).")
            return 1
        cabeceras = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

        periodos = cliente.get(SIRE_PERIODOS_RVIE_URL, headers=cabeceras)
        _mostrar("periodos RVIE habilitados (§5.2)", periodos)

        propuesta = cliente.get(
            SIRE_EXPORTAR_PROPUESTA_URL.format(periodo=args.periodo),
            params={"codTipoArchivo": 0},
            headers=cabeceras,
        )
        _mostrar(f"descargar propuesta RVIE {args.periodo} (§5.18)", propuesta)

    if propuesta.status_code == 200 and "numTicket" in str(_cuerpo(propuesta)):
        print("\nVEREDICTO: ACOGIDA — SUNAT generó el ticket de descarga.")
        return 0
    if propuesta.status_code == 422 and "1070" in str(_cuerpo(propuesta)):
        print(
            "\nVEREDICTO: ACOGIDA — el servicio respondió, pero el periodo no tiene "
            "comprobantes (error 1070). Pruebe otro periodo."
        )
        return 0
    if periodos.status_code == 200:
        print("\nVEREDICTO: el libro RVIE está habilitado, pero la descarga falló (ver arriba).")
        return 0
    print("\nVEREDICTO: NO CONFIRMADA — revise el cod/msg de SUNAT impreso arriba.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
