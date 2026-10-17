"""Pool de conexiones a Supabase (Postgres) con RLS aplicado por sesión.

Cada petición abre una transacción y, antes de tocar datos, fija los claims del
usuario y cambia al rol `authenticated` de Supabase. Así las políticas RLS se
evalúan con `auth.uid()` del usuario real y no con el rol dueño de la conexión.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from app.core.config import Settings
from app.core.errors import ConfigError, DatabaseNotConfiguredError
from app.core.logging import get_logger

logger = get_logger(__name__)

# Una sola consulta fija claims y rol: cada ida y vuelta a la base cuesta latencia y, con la
# base remota, eran 4 por petición. `set_config('role', ..., true)` equivale a `set local role`.
_SET_SESION_RLS = """
select set_config('request.jwt.claims', $1, true),
       set_config('request.jwt.claim.sub', $2, true),
       set_config('request.jwt.claim.role', $3, true),
       set_config('role', 'authenticated', true)
"""


async def _init_connection(connection: asyncpg.Connection) -> None:
    """Codifica json/jsonb como objetos Python en ambas direcciones."""
    for type_name in ("json", "jsonb"):
        await connection.set_type_codec(
            type_name,
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )


class Database:
    """Envoltura mínima sobre `asyncpg.Pool`."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: asyncpg.Pool | None = None

    @property
    def configured(self) -> bool:
        return self._settings.database_configured

    @property
    def connected(self) -> bool:
        return self._pool is not None

    async def connect(self) -> None:
        if self._pool is not None:
            return
        if not self._settings.database_configured:
            raise DatabaseNotConfiguredError()
        dsn = self._settings.database_url
        assert dsn is not None  # garantizado por database_configured
        self._pool = await asyncpg.create_pool(
            dsn=dsn.get_secret_value(),
            min_size=self._settings.database_pool_min_size,
            max_size=self._settings.database_pool_max_size,
            command_timeout=self._settings.database_command_timeout_seconds,
            init=_init_connection,
        )
        logger.info(
            "Pool de base de datos conectado (min=%s, max=%s)",
            self._settings.database_pool_min_size,
            self._settings.database_pool_max_size,
        )

    async def ping(self) -> None:
        """Comprueba que el pool responde (`select 1`).

        Se usa en el readiness: si la base no contesta, la instancia no debe
        recibir tráfico. No aplica RLS porque no lee datos del usuario.
        """
        if self._pool is None:
            if not self._settings.database_configured:
                raise DatabaseNotConfiguredError()
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as connection:
            await connection.fetchval("select 1")

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            logger.info("Pool de base de datos cerrado")

    @asynccontextmanager
    async def authenticated(self, claims: Mapping[str, Any]) -> AsyncIterator[asyncpg.Connection]:
        """Presta una conexión dentro de una transacción con los claims del usuario."""
        if self._pool is None:
            raise DatabaseNotConfiguredError()
        if not claims.get("sub"):
            raise ConfigError("No se puede abrir una sesión autenticada sin 'sub' en los claims")

        async with self._pool.acquire() as connection, connection.transaction():
            if self._settings.database_apply_rls:
                payload = json.dumps(dict(claims), default=str)
                await connection.execute(
                    _SET_SESION_RLS,
                    payload,
                    str(claims["sub"]),
                    str(claims.get("role", "authenticated")),
                )
            yield connection
