from __future__ import annotations

import json
from typing import Any

import pytest

from app.core.config import Settings
from app.core.errors import ConfigError, DatabaseNotConfiguredError
from app.db.pool import Database

SUB = "11111111-1111-1111-1111-111111111111"


class _TransaccionFalsa:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: Any) -> bool:
        return False


class _AdquirirFalso:
    def __init__(self, conexion: _ConexionFalsa) -> None:
        self._conexion = conexion

    async def __aenter__(self) -> _ConexionFalsa:
        return self._conexion

    async def __aexit__(self, *_: Any) -> bool:
        return False


class _ConexionFalsa:
    def __init__(self) -> None:
        self.ejecutados: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> _TransaccionFalsa:
        return _TransaccionFalsa()

    async def execute(self, consulta: str, *args: Any) -> str:
        self.ejecutados.append((consulta, args))
        return "SELECT 1"


class _PoolFalso:
    def __init__(self, conexion: _ConexionFalsa) -> None:
        self._conexion = conexion

    def acquire(self) -> _AdquirirFalso:
        return _AdquirirFalso(self._conexion)


def _database(**overrides: object) -> Database:
    base: dict[str, object] = {
        "_env_file": None,
        "database_url": "postgresql://usuario:clave@localhost:5432/postgres",
    }
    base.update(overrides)
    return Database(Settings(**base))


def _conectar_falso(database: Database, conexion: _ConexionFalsa) -> None:
    # Se inyecta el pool falso: evita necesitar un Postgres real en las pruebas.
    database._pool = _PoolFalso(conexion)


async def test_aplica_claims_y_rol_authenticated() -> None:
    database = _database()
    conexion = _ConexionFalsa()
    _conectar_falso(database, conexion)

    async with database.authenticated({"sub": SUB, "role": "authenticated"}) as prestada:
        assert prestada is conexion

    # Una sola ida y vuelta a la base fija claims y rol (antes eran cuatro).
    assert len(conexion.ejecutados) == 1
    sql, (claims, sub, rol) = conexion.ejecutados[0]
    assert "set_config('role', 'authenticated', true)" in sql
    assert "request.jwt.claims" in sql
    assert "request.jwt.claim.sub" in sql
    assert json.loads(claims)["sub"] == SUB
    assert sub == SUB
    assert rol == "authenticated"


async def test_sin_rls_no_cambia_el_rol() -> None:
    database = _database(database_apply_rls=False)
    conexion = _ConexionFalsa()
    _conectar_falso(database, conexion)

    async with database.authenticated({"sub": SUB}):
        pass

    assert conexion.ejecutados == []


async def test_sin_sub_no_abre_sesion() -> None:
    database = _database()
    _conectar_falso(database, _ConexionFalsa())
    with pytest.raises(ConfigError, match="sub"):
        async with database.authenticated({}):
            pass


async def test_sin_pool_configurado() -> None:
    database = _database()
    with pytest.raises(DatabaseNotConfiguredError):
        async with database.authenticated({"sub": SUB}):
            pass


async def test_connect_sin_database_url() -> None:
    database = Database(Settings(_env_file=None, database_url=""))
    assert database.configured is False
    with pytest.raises(DatabaseNotConfiguredError):
        await database.connect()
