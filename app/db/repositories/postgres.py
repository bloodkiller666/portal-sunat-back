"""Implementación de los repositorios sobre asyncpg.

Todas las consultas dependen de las políticas RLS: la sesión ya viene con los
claims del usuario y el rol `authenticated`.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg

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

_COLUMNAS_COMPROBANTE = """
    id, company_id, ruc_emisor, tipo_documento, serie, correlativo, numero, nombre_archivo,
    fecha_emision, moneda, monto_total, estado, sunat_ambiente, n_sap, orden_compra, check_sap,
    cdr_codigo, cdr_descripcion, cdr_notas, cdr_fecha, ultimo_envio_at, anulado_at,
    motivo_anulacion, created_at, updated_at, created_by
"""


def _row(record: asyncpg.Record | None) -> dict[str, Any] | None:
    return dict(record) if record is not None else None


class PostgresCompaniesRepository:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    async def list_for_user(self, user_id: UUID) -> list[CompanyMember]:
        records = await self._connection.fetch(
            """
            select c.id, c.ruc, c.razon_social, c.nombre_comercial, c.direccion, c.ubigeo,
                   c.activo, c.sunat_ambiente, c.certificado_alias, c.certificado_expira_at,
                   c.created_at, c.updated_at, uc.rol
            from public.user_companies uc
            join public.companies c on c.id = uc.company_id
            where uc.user_id = $1 and uc.activo and c.activo
            order by c.razon_social
            """,
            user_id,
        )
        return [
            CompanyMember(company=Company.model_validate(record), rol=record["rol"])
            for record in records
        ]

    async def get_by_ruc(self, ruc: str) -> Company | None:
        record = await self._connection.fetchrow(
            "select * from public.companies where ruc = $1", ruc
        )
        data = _row(record)
        return Company.model_validate(data) if data else None

    async def get_by_id(self, company_id: UUID) -> Company | None:
        record = await self._connection.fetchrow(
            "select * from public.companies where id = $1", company_id
        )
        data = _row(record)
        return Company.model_validate(data) if data else None

    async def get_membership(self, user_id: UUID, company_id: UUID) -> Membership | None:
        record = await self._connection.fetchrow(
            """
            select user_id, company_id, rol, activo
            from public.user_companies
            where user_id = $1 and company_id = $2
            """,
            user_id,
            company_id,
        )
        data = _row(record)
        return Membership.model_validate(data) if data else None


class PostgresComprobantesRepository:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    async def get(self, comprobante_id: UUID) -> Comprobante | None:
        record = await self._connection.fetchrow(
            f"select {_COLUMNAS_COMPROBANTE} from public.comprobantes where id = $1",
            comprobante_id,
        )
        data = _row(record)
        return Comprobante.model_validate(data) if data else None

    async def update_sunat_state(
        self,
        comprobante_id: UUID,
        *,
        estado: EstadoComprobante,
        cdr_codigo: str | None,
        cdr_descripcion: str | None,
        cdr_notas: list[str],
    ) -> Comprobante:
        record = await self._connection.fetchrow(
            """
            update public.comprobantes
               set estado = $2,
                   cdr_codigo = coalesce($3, cdr_codigo),
                   cdr_descripcion = coalesce($4, cdr_descripcion),
                   cdr_notas = case
                       when $5::jsonb = '[]'::jsonb then cdr_notas
                       else $5::jsonb
                   end,
                   cdr_fecha = case when $3 is null then cdr_fecha else now() end,
                   anulado_at = case
                       when $2 = 'anulado' then coalesce(anulado_at, now())
                       else anulado_at
                   end,
                   ultimo_envio_at = now(),
                   updated_at = now()
             where id = $1
            returning id, company_id, ruc_emisor, tipo_documento, serie, correlativo, numero,
                      nombre_archivo, fecha_emision, moneda, monto_total, estado, sunat_ambiente,
                      n_sap, orden_compra, check_sap, cdr_codigo, cdr_descripcion, cdr_notas,
                      cdr_fecha, ultimo_envio_at, anulado_at, motivo_anulacion, created_at,
                      updated_at, created_by
            """,
            comprobante_id,
            estado.value,
            cdr_codigo,
            cdr_descripcion,
            cdr_notas,
        )
        data = _row(record)
        if data is None:
            from app.core.errors import NotFoundError

            raise NotFoundError("El comprobante no existe o no pertenece a sus empresas")
        return Comprobante.model_validate(data)


class PostgresSunatEnviosRepository:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    async def record(self, registro: SunatEnvioRegistro) -> SunatEnvio:
        record = await self._connection.fetchrow(
            """
            insert into public.sunat_envios (
                company_id, comprobante_id, tipo_operacion, sunat_ambiente, endpoint, metodo,
                request_xml, response_xml, http_status, codigo_respuesta, mensaje_respuesta,
                notas, exito, error_tipo, duracion_ms
            )
            values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
            returning id, company_id, comprobante_id, tipo_operacion, sunat_ambiente, endpoint,
                      metodo, request_xml, response_xml, http_status, codigo_respuesta,
                      mensaje_respuesta, notas, exito, error_tipo, duracion_ms, created_at
            """,
            registro.company_id,
            registro.comprobante_id,
            registro.tipo_operacion.value,
            registro.sunat_ambiente.value,
            registro.endpoint,
            registro.metodo,
            registro.request_xml,
            registro.response_xml,
            registro.http_status,
            registro.codigo_respuesta,
            registro.mensaje_respuesta,
            registro.notas,
            registro.exito,
            registro.error_tipo,
            registro.duracion_ms,
        )
        data = _row(record)
        assert data is not None  # un insert con returning siempre devuelve fila
        return SunatEnvio.model_validate(data)

    async def list_for_comprobante(
        self, comprobante_id: UUID, *, limit: int = 20
    ) -> list[SunatEnvio]:
        records = await self._connection.fetch(
            """
            select id, company_id, comprobante_id, tipo_operacion, sunat_ambiente, endpoint,
                   metodo, request_xml, response_xml, http_status, codigo_respuesta,
                   mensaje_respuesta, notas, exito, error_tipo, duracion_ms, created_at
            from public.sunat_envios
            where comprobante_id = $1
            order by created_at desc
            limit $2
            """,
            comprobante_id,
            limit,
        )
        return [SunatEnvio.model_validate(_row(record)) for record in records]


class PostgresArchivosRepository:
    """Solo lectura por ahora: la carga a SharePoint es de una fase posterior."""

    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    async def list_for_comprobante(self, comprobante_id: UUID) -> list[Archivo]:
        records = await self._connection.fetch(
            "select * from public.archivos where comprobante_id = $1 order by created_at",
            comprobante_id,
        )
        return [Archivo.model_validate(_row(record)) for record in records]
