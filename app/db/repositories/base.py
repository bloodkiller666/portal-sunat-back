"""Interfaces de repositorio.

La API depende de estos Protocolos, no de asyncpg: en pruebas se inyectan
implementaciones en memoria (ver `tests/fakes.py`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable
from uuid import UUID

from app.core.enums import (
    CampoOrden,
    DireccionOrden,
    EstadoComprobante,
    OrigenComprobante,
    RespaldoFiltro,
    SunatAmbiente,
    TipoArchivo,
)
from app.db.models import (
    Archivo,
    ArchivoRegistro,
    AutomatizacionLog,
    AutomatizacionLogRegistro,
    BizlinksConsulta,
    BizlinksConsultaRegistro,
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


@dataclass(frozen=True, slots=True)
class FiltrosListado:
    """Criterios del listado de comprobantes (los comparten la página y el resumen)."""

    estado: EstadoComprobante | None = None
    origen: OrigenComprobante | None = None
    fuente: str | None = None
    fecha_desde: date | None = None
    fecha_hasta: date | None = None
    tipo_documento: str | None = None
    serie: str | None = None
    q: str | None = None
    n_sap: str | None = None
    vencimiento_desde: date | None = None
    vencimiento_hasta: date | None = None
    moneda: str | None = None
    respaldo: RespaldoFiltro | None = None
    orden: CampoOrden = CampoOrden.fecha
    direccion: DireccionOrden = DireccionOrden.desc


@dataclass(frozen=True, slots=True)
class FilaSap:
    """Una fila del Excel de N° SAP: documento (serie + correlativo), RUC opcional y N° SAP."""

    serie: str
    correlativo: int
    n_sap: str
    ruc: str | None = None


@dataclass(frozen=True, slots=True)
class PaginaComprobantes:
    """Una página del listado. `total` es `None` si no se pidió contar."""

    items: list[Comprobante]
    total: int | None = None
    # Respaldos subidos (xml, cdr, pdf) de cada fila, resueltos junto con la página.
    tipos: dict[UUID, set[TipoArchivo]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ResumenComprobantes:
    """Agregados del conjunto filtrado, calculados en la base (no página a página)."""

    total: int = 0
    por_estado: dict[EstadoComprobante, int] = field(default_factory=dict)
    # Neto por moneda: las notas de crédito restan; sin anulados ni rechazados.
    facturado: dict[str, Decimal] = field(default_factory=dict)
    # Cobertura de respaldo sobre lo exigible (todo menos anulados).
    exigibles: int = 0
    con_xml: int = 0
    con_cdr: int = 0
    con_pdf: int = 0


@runtime_checkable
class CompaniesRepository(Protocol):
    async def list_for_user(self, user_id: UUID) -> list[CompanyMember]: ...

    async def get_by_ruc(self, ruc: str) -> Company | None: ...

    async def get_by_id(self, company_id: UUID) -> Company | None: ...

    async def get_membership(self, user_id: UUID, company_id: UUID) -> Membership | None: ...

    async def get_access(
        self, ruc: str, user_id: UUID
    ) -> tuple[Company, Membership | None] | None:
        """Empresa por RUC y la membresía del usuario en una sola consulta (`None` si no existe)."""
        ...


@runtime_checkable
class ComprobantesRepository(Protocol):
    async def get(self, comprobante_id: UUID) -> Comprobante | None: ...

    async def list_for_company(
        self,
        company_id: UUID,
        filtros: FiltrosListado | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
        contar: bool = False,
    ) -> PaginaComprobantes:
        """Página ordenada. Con `contar` devuelve además el total del conjunto filtrado."""
        ...

    async def resumen(
        self, company_id: UUID, filtros: FiltrosListado | None = None
    ) -> ResumenComprobantes: ...

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
        ruc_emisor: str | None = None,
    ) -> Comprobante | None:
        """Con `ruc_emisor` distingue a dos proveedores que emitieron la misma serie-número."""
        ...

    async def find_recibidas(
        self, company_id: UUID, *, ruc_emisor: str, serie: str, correlativo: int
    ) -> list[Comprobante]:
        """Compras de un proveedor con esa serie-número (sirve para unir un PDF suelto)."""
        ...

    async def find_emitido(
        self, company_id: UUID, *, tipo_documento: str, serie: str, correlativo: int
    ) -> Comprobante | None:
        """Comprobante emitido por clave, en cualquier ambiente (prefiere producción)."""
        ...

    async def completar_desde_xml(
        self, comprobante_id: UUID, registro: ComprobanteRegistro
    ) -> Comprobante: ...

    async def registrar_correo(
        self,
        company_id: UUID,
        *,
        ruc_emisor: str,
        serie: str,
        correlativo: int,
        asunto: str,
        remitente: str | None,
        recibido_at: datetime,
    ) -> int:
        """Anota de qué correo llegó una compra; si ya tenía otro, gana el que llegó primero.

        Devuelve cuántas filas se actualizaron (0 si la compra no existe o ya tenía un correo
        anterior).
        """
        ...

    async def actualizar_pago(
        self,
        comprobante_id: UUID,
        *,
        fecha_vencimiento: date | None,
        forma_pago: str | None,
    ) -> Comprobante:
        """El XML de respaldo completa el vencimiento y la forma de pago que SIRE no trae.

        Un valor `None` conserva el que ya tenía el comprobante (coalesce).
        """
        ...

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
        n_sap: str | None = None,
    ) -> Comprobante: ...

    async def asignar_n_sap_lote(self, company_id: UUID, filas: list[FilaSap]) -> set[int]:
        """Asigna el N° SAP a los comprobantes de la empresa que coincidan con cada fila.

        Devuelve las posiciones de `filas` que encontraron comprobante; el resto no existe
        en el portal y se omite sin error.
        """
        ...


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
class BizlinksConsultasRepository(Protocol):
    """Bitácora de consultas al OSE: topes diarios, cortacircuito y auditoría."""

    async def registrar(self, registro: BizlinksConsultaRegistro) -> BizlinksConsulta: ...

    async def ultimo_bloqueo_desde(
        self, company_id: UUID, canal: str, desde: datetime
    ) -> datetime | None: ...

    async def comprobantes_consultados_desde(
        self, company_id: UUID, canal: str, desde: datetime
    ) -> int: ...

    async def sin_exito_desde(
        self, company_id: UUID, comprobante_ids: list[UUID], canal: str, desde: datetime
    ) -> set[UUID]:
        """IDs consultados sin éxito desde `desde` (no se reintentan todavía)."""
        ...


@runtime_checkable
class ArchivosRepository(Protocol):
    """Referencias de archivos alojados en Cloudflare R2 (bucket privado)."""

    async def get(self, archivo_id: UUID) -> Archivo | None: ...

    async def list_for_comprobante(self, comprobante_id: UUID) -> list[Archivo]: ...

    async def tipos_por_comprobante(
        self, comprobante_ids: list[UUID]
    ) -> dict[UUID, set[TipoArchivo]]:
        """Tipos de archivo ya subidos (xml, cdr, pdf...) de cada comprobante."""
        ...

    async def list_evidencias(self, company_id: UUID, *, tipo: TipoArchivo) -> list[Archivo]:
        """Archivos de la empresa que no cuelgan de un comprobante (p. ej. ZIP de SIRE)."""
        ...

    async def registrar(self, registro: ArchivoRegistro) -> Archivo: ...


@runtime_checkable
@runtime_checkable
class AutomatizacionLogsRepository(Protocol):
    """Corridas de la automatización diaria (solo agregado; se leen en el apartado Logs)."""

    async def registrar(self, registro: AutomatizacionLogRegistro) -> AutomatizacionLog: ...

    async def listar(
        self, company_id: UUID, *, resultado: str | None, limit: int, offset: int
    ) -> tuple[list[AutomatizacionLog], int]:
        """Las más recientes primero, con el total del conjunto filtrado."""
        ...


class UnitOfWork(Protocol):
    """Agrupa los repositorios que comparten la misma conexión/transacción."""

    companies: CompaniesRepository
    comprobantes: ComprobantesRepository
    items: ComprobanteItemsRepository
    sunat_envios: SunatEnviosRepository
    archivos: ArchivosRepository
    bizlinks_consultas: BizlinksConsultasRepository
    automatizacion_logs: AutomatizacionLogsRepository
