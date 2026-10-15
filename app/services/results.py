"""Modelos de respuesta de la API.

Se definen junto a los servicios (que son quienes los construyen) para que la
capa HTTP solo los exponga. Ninguno incluye secretos: de las credenciales solo se
informa su presencia.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.enums import (
    EstadoArchivo,
    EstadoComprobante,
    OrigenComprobante,
    RolEmpresa,
    SunatAmbiente,
    TipoArchivo,
    TipoOperacionSunat,
)


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


class ReadyResponse(BaseModel):
    """Readiness para el orquestador: base de datos y verificación de JWT."""

    status: Literal["ready", "not_ready"]
    database: Literal["ok", "not_configured", "unreachable"]
    auth_configured: bool
    sunat_env: SunatAmbiente
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


class ComprobanteOut(BaseModel):
    """Comprobante listo para pintar en el portal (sin datos de SUNAT sensibles)."""

    id: UUID
    company_id: UUID
    origen: OrigenComprobante = OrigenComprobante.emitido
    origen_label: str = "Emitido"
    ruc_emisor: str
    ruc_receptor: str | None = None
    razon_social_emisor: str | None = None
    razon_social_receptor: str | None = None
    tipo_documento: str
    serie: str
    correlativo: int
    numero: str
    nombre_archivo: str
    fecha_emision: date | None = None
    fecha_vencimiento: date | None = None
    forma_pago: str | None = None
    fuente: str = "xml"
    moneda: str = "PEN"
    monto_total: Decimal | None = None
    estado: EstadoComprobante
    estado_label: str
    sunat_ambiente: SunatAmbiente
    n_sap: str | None = None
    orden_compra: str | None = None
    check_sap: bool = False
    cdr_codigo: str | None = None
    cdr_descripcion: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ComprobantesPage(BaseModel):
    items: list[ComprobanteOut]
    limit: int
    offset: int
    has_more: bool


class ItemOut(BaseModel):
    """Una línea del detalle del comprobante (`comprobante_items`)."""

    item_nro: int
    descripcion: str
    cantidad: Decimal
    unidad_medida: str | None = None
    codigo: str | None = None
    valor_unitario: Decimal | None = None
    precio_unitario: Decimal | None = None
    descuento: Decimal = Decimal("0")
    igv: Decimal = Decimal("0")
    isc: Decimal = Decimal("0")
    tip_afectacion_igv: str | None = None
    total: Decimal | None = None


class ComprobanteDetalleOut(ComprobanteOut):
    """Comprobante con su detalle de ítems (vista del modal de detalle)."""

    items: list[ItemOut] = Field(default_factory=list)


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


class RefreshLoteItemOut(BaseModel):
    """Resultado del refresco de un comprobante dentro de un lote."""

    comprobante_id: UUID
    numero: str
    estado_anterior: EstadoComprobante
    estado_nuevo: EstadoComprobante
    estado_label: str
    cambio_estado: bool
    ok: bool = True
    cdr_codigo: str | None = None
    mensaje: str | None = None
    error_code: str | None = None


class RefreshLoteOut(BaseModel):
    """Resumen del refresco masivo de los comprobantes pendientes de la empresa."""

    ruc: str
    total: int
    procesados: int
    con_cambio: int
    fallidos: int
    detenido_por: str | None = None
    items: list[RefreshLoteItemOut]
    consultado_at: datetime


class PaqueteItemOut(BaseModel):
    """Resultado de un archivo dentro del paquete importado."""

    archivo: str
    ok: bool = True
    comprobante_id: UUID | None = None
    numero: str | None = None
    pdf: bool = False
    code: str | None = None
    mensaje: str | None = None


class PaqueteOut(BaseModel):
    """Resumen de la importación masiva de un paquete ZIP."""

    ruc: str
    origen: OrigenComprobante
    # Particiones de `total`: importados + duplicados + con_error + sin_xml.
    total: int
    importados: int
    con_pdf: int
    duplicados: int
    con_error: int
    sin_xml: int
    items: list[PaqueteItemOut]
    procesado_at: datetime


class EnviosResponse(BaseModel):
    """Bitácora de intentos contra SUNAT de un comprobante (sin tramas XML)."""

    comprobante_id: UUID
    items: list[IntentoOut]
    total: int


class ArchivoOut(BaseModel):
    """Referencia de un archivo en el almacenamiento (sin la URL firmada)."""

    id: UUID
    comprobante_id: UUID | None = None
    tipo: TipoArchivo
    nombre: str
    mime_type: str | None = None
    size_bytes: int | None = None
    hash_sha256: str | None = None
    estado: EstadoArchivo
    subido_at: datetime | None = None
    created_at: datetime | None = None


class ArchivosPage(BaseModel):
    comprobante_id: UUID
    items: list[ArchivoOut]
    total: int


class DescargaOut(BaseModel):
    """URL firmada y temporal para descargar un archivo (bucket privado)."""

    archivo_id: UUID
    nombre: str
    url: str
    expires_in: int
    expira_at: datetime


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


class SireSyncOut(BaseModel):
    """Resultado de sincronizar las ventas de un periodo desde SIRE."""

    periodo: str
    ticket: str | None = None
    estado: Literal["completado", "en_proceso", "sin_datos"]
    total_filas: int = 0
    nuevos: int = 0
    ya_existian: int = 0
    anulados: int = 0
    omitidos: dict[str, int] = Field(default_factory=dict)
    mensaje: str
