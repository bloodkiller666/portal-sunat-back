"""Modelos de respuesta de la API.

Se definen junto a los servicios (que son quienes los construyen) para que la
capa HTTP solo los exponga. Ninguno incluye secretos: de las credenciales solo se
informa su presencia.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.enums import EstadoComprobante, RolEmpresa, SunatAmbiente, TipoOperacionSunat


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    app: str
    version: str
    environment: str
    sunat_env: SunatAmbiente
    confirm_prod_emission: bool
    database_configured: bool
    auth_configured: bool
    timestamp: datetime


class CompanyOut(BaseModel):
    id: UUID
    ruc: str
    razon_social: str
    nombre_comercial: str | None = None
    activo: bool = True
    sunat_ambiente: SunatAmbiente | None = Field(
        default=None, description="NULL significa que rige el ambiente global (SUNAT_ENV)"
    )
    rol: RolEmpresa


class CompaniesResponse(BaseModel):
    items: list[CompanyOut]
    total: int


class CertificateOut(BaseModel):
    """Metadatos del certificado digital. Nunca incluye la clave ni el archivo."""

    configurado: bool
    abre_con_clave: bool
    origen: str | None = None
    sujeto: str | None = None
    emisor: str | None = None
    numero_serie: str | None = None
    valido_desde: datetime | None = None
    valido_hasta: datetime | None = None
    dias_para_vencer: int | None = None
    vigente: bool = False
    error: str | None = None


class CredencialesOut(BaseModel):
    sol_user_configurado: bool
    api_rest_configurada: bool
    certificado: CertificateOut


class DetalleSunatOut(BaseModel):
    codigo: str | None = None
    mensaje: str | None = None


class SunatStatusOut(BaseModel):
    """Resultado de la prueba de conexión y credenciales."""

    ruc: str
    razon_social: str
    sunat_env: SunatAmbiente
    estado: Literal[
        "autenticado",
        "credenciales_invalidas",
        "servicio_no_disponible",
        "sin_credenciales",
    ]
    autenticado: bool
    metodo: str | None = None
    endpoint: str | None = None
    detalle: DetalleSunatOut | None = None
    credenciales: CredencialesOut
    latencia_ms: int | None = None
    intento_id: int | None = None
    consultado_at: datetime


class CdrOut(BaseModel):
    codigo: str
    descripcion: str
    notas: list[str] = Field(default_factory=list)
    archivo: str | None = None
    identificador: str | None = None
    fecha_respuesta: datetime | None = None


class IntentoOut(BaseModel):
    """Resumen del intento registrado en `sunat_envios` (sin tramas XML)."""

    id: int
    tipo_operacion: TipoOperacionSunat
    exito: bool
    http_status: int | None = None
    codigo: str | None = None
    mensaje: str | None = None
    error_tipo: str | None = None
    duracion_ms: int | None = None
    created_at: datetime | None = None


class RefreshCdrOut(BaseModel):
    comprobante_id: UUID
    numero: str
    estado_anterior: EstadoComprobante
    estado_nuevo: EstadoComprobante
    estado_label: str
    cambio_estado: bool
    cdr: CdrOut | None = None
    mensaje: str | None = None
    sunat_env: SunatAmbiente
    intento: IntentoOut | None = None
    consultado_at: datetime
