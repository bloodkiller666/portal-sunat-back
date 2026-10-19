"""Unidad de trabajo: repositorios que comparten una conexión autenticada."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any

from app.db.pool import Database
from app.db.repositories.postgres import (
    PostgresArchivosRepository,
    PostgresBizlinksConsultasRepository,
    PostgresCompaniesRepository,
    PostgresComprobanteItemsRepository,
    PostgresComprobantesRepository,
    PostgresSunatEnviosRepository,
)

UowFactory = Callable[[Mapping[str, Any]], AbstractAsyncContextManager]


@dataclass(slots=True)
class PostgresUnitOfWork:
    companies: PostgresCompaniesRepository
    comprobantes: PostgresComprobantesRepository
    items: PostgresComprobanteItemsRepository
    sunat_envios: PostgresSunatEnviosRepository
    archivos: PostgresArchivosRepository
    bizlinks_consultas: PostgresBizlinksConsultasRepository


def build_uow_factory(database: Database) -> UowFactory:
    """Crea la fábrica que abre una sesión autenticada por petición."""

    @asynccontextmanager
    async def factory(claims: Mapping[str, Any]) -> AsyncIterator[PostgresUnitOfWork]:
        async with database.authenticated(claims) as connection:
            yield PostgresUnitOfWork(
                companies=PostgresCompaniesRepository(connection),
                comprobantes=PostgresComprobantesRepository(connection),
                items=PostgresComprobanteItemsRepository(connection),
                sunat_envios=PostgresSunatEnviosRepository(connection),
                archivos=PostgresArchivosRepository(connection),
                bizlinks_consultas=PostgresBizlinksConsultasRepository(connection),
            )

    return factory
