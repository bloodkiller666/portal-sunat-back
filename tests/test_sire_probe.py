"""Pruebas de `scripts.sire_probe` con SUNAT mockeado (ninguna toca la red)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from scripts.sire_probe import main

from app.sunat.endpoints import SIRE_EXPORTAR_PROPUESTA_URL, SIRE_PERIODOS_RVIE_URL, SIRE_TOKEN_URL

RUC = "20514831620"
CLIENT_ID = "11111111-2222-3333-4444-555555555555"
CLIENT_SECRET = "secreto-de-prueba-no-real"
CLAVE_SOL = "clave-sol-de-prueba"
TOKEN = "token-de-prueba-abcdefghijklmnop"

TOKEN_URL = SIRE_TOKEN_URL.format(client_id=CLIENT_ID)
PROPUESTA_URL = SIRE_EXPORTAR_PROPUESTA_URL.format(periodo="202609")


@pytest.fixture
def env_file(tmp_path: Path) -> str:
    ruta = tmp_path / ".env"
    ruta.write_text(
        "\n".join(
            [
                f"SUNAT_CRED_{RUC}_SOL_USER=USUARIO1",
                f"SUNAT_CRED_{RUC}_SOL_PASSWORD={CLAVE_SOL}",
                f"SUNAT_CRED_{RUC}_CLIENT_ID={CLIENT_ID}",
                f"SUNAT_CRED_{RUC}_CLIENT_SECRET={CLIENT_SECRET}",
            ]
        ),
        encoding="utf-8",
    )
    return str(ruta)


def _ejecutar(env_file: str) -> int:
    return main(["--ruc", RUC, "--periodo", "202609", "--env-file", env_file])


@respx.mock
def test_empresa_acogida_devuelve_ticket(env_file: str, capsys: pytest.CaptureFixture[str]) -> None:
    ruta_token = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": TOKEN, "expires_in": 3600})
    )
    respx.get(SIRE_PERIODOS_RVIE_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.get(PROPUESTA_URL).mock(
        return_value=httpx.Response(200, json={"numTicket": "20260100000001"})
    )

    assert _ejecutar(env_file) == 0

    salida = capsys.readouterr().out
    assert "ACOGIDA" in salida
    # El token SOAP del manual: password grant, scope SIRE, usuario = RUC + SOL.
    cuerpo = ruta_token.calls.last.request.content.decode()
    assert "grant_type=password" in cuerpo
    assert "scope=https%3A%2F%2Fapi-sire.sunat.gob.pe" in cuerpo
    assert f"username={RUC}USUARIO1" in cuerpo
    # Regla 1: ningún secreto ni el token llegan a la salida.
    for secreto in (CLIENT_SECRET, CLAVE_SOL, TOKEN):
        assert secreto not in salida


@respx.mock
def test_periodo_sin_comprobantes_sigue_siendo_acogida(env_file: str) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": TOKEN}))
    respx.get(SIRE_PERIODOS_RVIE_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.get(PROPUESTA_URL).mock(
        return_value=httpx.Response(
            422, json={"cod": "422", "errors": [{"cod": "1070", "msg": "Sin comprobantes"}]}
        )
    )

    assert _ejecutar(env_file) == 0


@respx.mock
def test_credenciales_rechazadas_no_continua(
    env_file: str, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid_client"})
    )
    periodos = respx.get(SIRE_PERIODOS_RVIE_URL).mock(return_value=httpx.Response(200))

    assert _ejecutar(env_file) == 1

    assert not periodos.called
    assert "no se pudo autenticar" in capsys.readouterr().out


@respx.mock
def test_empresa_no_acogida_se_reporta_sin_confirmar(
    env_file: str, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": TOKEN}))
    respx.get(SIRE_PERIODOS_RVIE_URL).mock(
        return_value=httpx.Response(403, json={"cod": "403", "msg": "Forbidden"})
    )
    respx.get(PROPUESTA_URL).mock(
        return_value=httpx.Response(403, json={"cod": "403", "msg": "Forbidden"})
    )

    assert _ejecutar(env_file) == 1

    salida = capsys.readouterr().out
    assert "NO CONFIRMADA" in salida
    assert TOKEN not in salida


def test_sin_client_id_falla_sin_red(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ruta = tmp_path / ".env"
    ruta.write_text(
        f"SUNAT_CRED_{RUC}_SOL_USER=U\nSUNAT_CRED_{RUC}_SOL_PASSWORD=clave-larga-123\n",
        encoding="utf-8",
    )

    assert main(["--ruc", RUC, "--env-file", str(ruta)]) == 1
    assert "CLIENT_ID" in capsys.readouterr().out
