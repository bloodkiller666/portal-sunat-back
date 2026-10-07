"""Interfaces de repositorio.

La API depende de estos Protocolos, no de asyncpg: en pruebas se inyectan
implementaciones en memoria (ver `tests/fakes.py`).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from app.core.enums import EstadoComprobante
from app.db.models import (
    Archivo,
    Company,
    CompanyMember,
    Comprobante,
    Membership,
    SunatEnvio,
    SunatEnvioRegistro,
)


@runtime_checkable
class CompaniesRepository(Protocol):
    async def list_for_user(self, user_id: UUID) -> list[CompanyMember]: ...

    async def get_by_ruc(self, ruc: str) -> Company | None: ...

    async def get_by_id(self, company_id: UUID) -> Company | None: ...

    async def get_membership(self, user_id: UUID, company_id: UUID) -> Membership | None: ...


@runtime_checkable
class ComprobantesRepository(Protocol):
    async def get(self, comprobante_id: UUID) -> Comprobante | None: ...

    async def update_sunat_state(
        self,
        comprobante_id: UUID,
        *,
        estado: EstadoComprobante,
        cdr_codigo: str | None,
        cdr_descripcion: str | None,
        cdr_notas: list[str],
    ) -> Comprobante: ...


@runtime_checkable
class SunatEnviosRepository(Protocol):
    async def record(self, registro: SunatEnvioRegistro) -> SunatEnvio: ...

    async def list_for_comprobante(
        self, comprobante_id: UUID, *, limit: int = 20
    ) -> list[SunatEnvio]: ...


@runtime_checkable
class ArchivosRepository(Protocol):
    """Preparado para la fase de SharePoint (fuera de alcance)."""

    async def list_for_comprobante(self, comprobante_id: UUID) -> list[Archivo]: ...


@runtime_checkable
class UnitOfWork(Protocol):
    """Agrupa los repositorios que comparten la misma conexión/transacción."""

    companies: CompaniesRepository
    comprobantes: ComprobantesRepository
    sunat_envios: SunatEnviosRepository
    archivos: ArchivosRepository
