"""Diagnóstico de credenciales y servicios SUNAT (sin tocar la base de datos).

Carga `.env` como lo hace `fastapi dev`, resuelve las credenciales de la empresa
por RUC y prueba el cliente SOAP real. Útil para validar una cuenta SOL o para
corregir el mapeo de estados con una respuesta real.

Ejemplos:

    # ¿Autentican las credenciales en beta y/o producción?
    uv run python -m scripts.sunat_probe --ruc 20514831620 --env both

    # Consultar un comprobante real y ver el estado mapeado para el frontend
    uv run python -m scripts.sunat_probe --ruc 20514831620 --env prod \
        --tipo 01 --serie F001 --numero 1

Se ejecuta desde la raíz del proyecto (o con `python -m scripts.sunat_probe`).

Nunca imprime la clave SOL ni el usuario SOL; los mensajes pasan por `redact`.
Sale con código 0 si al menos un ambiente autenticó, 1 si ninguno.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.core.config import Settings
from app.core.enums import SunatAmbiente
from app.core.errors import CredentialsNotConfiguredError
from app.core.redaction import redact
from app.secrets.env import EnvSecretsProvider
from app.sunat.consult import SunatConsultClient
from app.sunat.states import map_codigo_a_estado


def cargar_env(ruta: Path) -> dict[str, str]:
    """Lee un `.env` a un mapping (sin exportarlo al proceso)."""
    mapping: dict[str, str] = {}
    if not ruta.exists():
        return mapping
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        mapping[clave.strip()] = valor.strip()
    return mapping


def _probar_ambiente(settings: Settings, credenciales, env: SunatAmbiente) -> bool:
    print(f"\n===== ambiente {env.value} =====")
    try:
        cliente = SunatConsultClient(credenciales, settings, env=env)
        resultado = cliente.check_availability()
    except Exception as exc:
        print("EXCEPCION:", type(exc).__name__, redact(str(exc))[:300])
        return False

    print("autenticado:", resultado.autenticado)
    print("metodo:", resultado.metodo)
    print("endpoint:", resultado.endpoint)
    print("codigo:", resultado.codigo)
    print("mensaje:", redact(resultado.mensaje) if resultado.mensaje else None)
    print("error_tipo:", resultado.error_tipo)
    print("http_status:", resultado.http_status, "| duracion_ms:", resultado.duracion_ms)
    return resultado.autenticado


def _consultar_comprobante(
    settings: Settings,
    credenciales,
    env: SunatAmbiente,
    *,
    tipo: str,
    serie: str,
    numero: str,
) -> None:
    """Consulta estado/CDR y muestra el estado que vería el frontend."""
    print(f"\n===== consulta de comprobante ({env.value}) =====")
    try:
        cliente = SunatConsultClient(credenciales, settings, env=env)
        resultado = cliente.get_status_cdr(
            tipo_comprobante=tipo, serie=serie, numero=numero
        )
    except Exception as exc:
        print("EXCEPCION:", type(exc).__name__, redact(str(exc))[:300])
        return

    codigo = resultado.cdr.codigo if resultado.cdr else resultado.estado.codigo
    descripcion = resultado.cdr.descripcion if resultado.cdr else resultado.estado.mensaje
    estado = map_codigo_a_estado(codigo, descripcion)
    print("codigo:", codigo)
    print("descripcion:", redact(descripcion) if descripcion else None)
    print("estado_frontend:", estado.value, "|", estado.etiqueta)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnóstico de SUNAT")
    parser.add_argument("--ruc", required=True, help="RUC de la empresa (11 dígitos)")
    parser.add_argument(
        "--env",
        choices=["beta", "prod", "both"],
        default="both",
        help="Ambiente(s) a probar",
    )
    parser.add_argument("--tipo", help="Tipo de comprobante (01, 03, 07, 08)")
    parser.add_argument("--serie", help="Serie del comprobante (p. ej. F001)")
    parser.add_argument("--numero", help="Número/correlativo del comprobante")
    parser.add_argument("--env-file", default=".env", help="Ruta del archivo .env")
    args = parser.parse_args(argv)

    settings = Settings(_env_file=args.env_file)
    env_mapping = cargar_env(Path(args.env_file))
    try:
        credenciales = EnvSecretsProvider(env_mapping).get_company_credentials(args.ruc)
    except CredentialsNotConfiguredError as exc:
        print("ERROR:", exc.message)
        return 1

    print(f"RUC: {credenciales.ruc} | certificado configurado: {credenciales.has_certificate}")
    print(
        "servicios SUNAT para funciones: la cuenta NO expone una lista de funciones; "
        "solo se puede verificar qué servicio web acepta las credenciales."
    )

    if args.tipo and args.serie and args.numero:
        ambientes = [SunatAmbiente.beta, SunatAmbiente.prod] if args.env == "both" else [
            SunatAmbiente(args.env)
        ]
        for env in ambientes:
            _consultar_comprobante(
                settings,
                credenciales,
                env,
                tipo=args.tipo,
                serie=args.serie,
                numero=args.numero,
            )
        return 0

    ambientes = (
        [SunatAmbiente.beta, SunatAmbiente.prod]
        if args.env == "both"
        else [SunatAmbiente(args.env)]
    )
    autenticados = [_probar_ambiente(settings, credenciales, env) for env in ambientes]
    return 0 if any(autenticados) else 1


if __name__ == "__main__":
    sys.exit(main())
