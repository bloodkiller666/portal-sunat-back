"""Interfaces de repositorio.

La API depende de estos Protocolos, no de asyncpg: en pruebas se inyectan
implementaciones en memoria (ver `tests/fakes.py`).
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable
from uuid import UUID

from app.core.enums import EstadoComprobante, OrigenComprobante, SunatAmbiente
from app.db.models import (
    Archivo,
    ArchivoRegistro,
    Company,
    CompanyMember,
    Comprobante,
    ComprobanteItem,
    ComprobanteItemRegistro,
    ComprobanteRegistro,
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

    async def list_for_company(
        self,
        company_id: UUID,
        *,
        estado: EstadoComprobante | None = None,
        origen: OrigenComprobante | None = None,
        fecha_desde: date | None = None,
        fecha_hasta: date | None = None,
        tipo_documento: str | None = None,
        serie: str | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Comprobante]: ...

    async def create(self, registro: ComprobanteRegistro) -> Comprobante: ...

    async def create_if_absent(self, registro: ComprobanteRegistro) -> Comprobante | None: ...

    async def find_by_key(
        self,
        company_id: UUID,
        *,
        origen: OrigenComprobante,
        tipo_documento: str,
        serie: str,
        correlativo: int,
        sunat_ambiente: SunatAmbiente,
    ) -> Comprobante | None: ...

    async def completar_desde_xml(
        self, comprobante_id: UUID, registro: ComprobanteRegistro
    ) -> Comprobante: ...

    async def update_sunat_state(
        self,
        comprobante_id: UUID,
        *,
        estado: EstadoComprobante,
        cdr_codigo: str | None,
        cdr_descripcion: str | None,
        cdr_notas: list[str],
    ) -> Comprobante: ...

    async def update_sap(
        self,
        comprobante_id: UUID,
        *,
        check_sap: bool | None = None,
        n_sap: str | None = None,
        cambiar_n_sap: bool = False,
    ) -> Comprobante: ...


@runtime_checkable
class ComprobanteItemsRepository(Protocol):
    """Líneas del detalle de un comprobante (`comprobante_items`)."""

    async def create_many(
        self, registros: list[ComprobanteItemRegistro]
    ) -> list[ComprobanteItem]: ...

    async def list_for_comprobante(self, comprobante_id: UUID) -> list[ComprobanteItem]: ...


@runtime_checkable
class SunatEnviosRepository(Protocol):
    async def record(self, registro: SunatEnvioRegistro) -> SunatEnvio: ...

    async def list_for_comprobante(
        self, comprobante_id: UUID, *, limit: int = 20
    ) -> list[SunatEnvio]: ...


@runtime_checkable
class ArchivosRepository(Protocol):
    """Referencias de archivos alojados en Cloudflare R2 (bucket privado)."""

    async def get(self, archivo_id: UUID) -> Archivo | None: ...

    async def list_for_comprobante(self, comprobante_id: UUID) -> list[Archivo]: ...

    async def registrar(self, registro: ArchivoRegistro) -> Archivo: ...


@runtime_checkable
class UnitOfWork(Protocol):
    """Agrupa los repositorios que comparten la misma conexión/transacción."""

    companies: CompaniesRepository
    comprobantes: ComprobantesRepository
    items: ComprobanteItemsRepository
    sunat_envios: SunatEnviosRepository
    archivos: ArchivosRepository
