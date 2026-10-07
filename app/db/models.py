"""Modelos de las tablas de Supabase.

Los secretos (clave SOL, certificado) NO tienen columna aquí: se resuelven por
empresa con el `SecretsProvider` (regla 3).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.core.enums import (
    EstadoArchivo,
    EstadoComprobante,
    RolEmpresa,
    SunatAmbiente,
    TipoArchivo,
    TipoOperacionSunat,
)


class RowModel(BaseModel):
    """Base para filas: tolera columnas extra cuando el esquema evoluciona."""

    model_config = ConfigDict(extra="ignore", from_attributes=True)


class Company(RowModel):
    id: UUID
    ruc: str
    razon_social: str
    nombre_comercial: str | None = None
    direccion: str | None = None
    ubigeo: str | None = None
    activo: bool = True
    sunat_ambiente: SunatAmbiente | None = None
    certificado_alias: str | None = None
    certificado_expira_at: date | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class Membership(RowModel):
    user_id: UUID
    company_id: UUID
    rol: RolEmpresa
    activo: bool = True


class CompanyMember(RowModel):
    """Empresa + rol del usuario autenticado."""

    company: Company
    rol: RolEmpresa

    @property
    def ruc(self) -> str:
        return self.company.ruc

    @property
    def company_id(self) -> UUID:
        return self.company.id


class Comprobante(RowModel):
    id: UUID
    company_id: UUID
    ruc_emisor: str
    tipo_documento: str
    serie: str
    correlativo: int
    numero: str = ""
    nombre_archivo: str = ""
    fecha_emision: date | None = None
    moneda: str = "PEN"
    monto_total: Decimal | None = None
    estado: EstadoComprobante = EstadoComprobante.pendiente
    sunat_ambiente: SunatAmbiente = SunatAmbiente.beta
    n_sap: str | None = None
    orden_compra: str | None = None
    check_sap: bool = False
    cdr_codigo: str | None = None
    cdr_descripcion: str | None = None
    cdr_notas: list[str] = []
    cdr_fecha: datetime | None = None
    ultimo_envio_at: datetime | None = None
    anulado_at: datetime | None = None
    motivo_anulacion: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None

    @property
    def esta_anulado(self) -> bool:
        return self.estado is EstadoComprobante.anulado


class SunatEnvioRegistro(RowModel):
    """Datos para registrar un intento en `sunat_envios`. Nunca lleva secretos."""

    company_id: UUID
    comprobante_id: UUID | None = None
    tipo_operacion: TipoOperacionSunat
    sunat_ambiente: SunatAmbiente
    endpoint: str | None = None
    metodo: str | None = None
    request_xml: str | None = None
    response_xml: str | None = None
    http_status: int | None = None
    codigo_respuesta: str | None = None
    mensaje_respuesta: str | None = None
    notas: list[str] = []
    exito: bool = False
    error_tipo: str | None = None
    duracion_ms: int | None = None


class SunatEnvio(SunatEnvioRegistro):
    id: int
    created_at: datetime | None = None


class Archivo(RowModel):
    id: UUID
    company_id: UUID
    comprobante_id: UUID | None = None
    tipo: TipoArchivo
    nombre: str
    mime_type: str | None = None
    size_bytes: int | None = None
    hash_sha256: str | None = None
    sharepoint_drive_id: str | None = None
    sharepoint_item_id: str | None = None
    sharepoint_web_url: str | None = None
    sharepoint_path: str | None = None
    estado: EstadoArchivo = EstadoArchivo.pendiente
    error_detalle: str | None = None
    subido_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
