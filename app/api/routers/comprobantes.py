"""Endpoints de comprobantes: listado, importación de XML y refresco del CDR."""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, Path, Query, Response, UploadFile, status
from pydantic import BaseModel, Field, model_validator

from app.api.deps import (
    AcessoOperativo,
    CompanyAccessDep,
    CurrentUserDep,
    SecretsProviderDep,
    SettingsDep,
    SunatClientFactoryDep,
    UowDep,
)
from app.api.uploads import leer_con_limite
from app.core.enums import EstadoComprobante, OrigenComprobante
from app.services.comprobantes import (
    MAX_COMPROBANTES_PAGINA,
    MAX_INTENTOS_PAGINA,
    MAX_LOTE_REFRESCO,
    TAMANO_MAXIMO_XML,
    actualizar_sap,
    generar_pdf_comprobante,
    import_comprobante_xml,
    list_comprobantes,
    list_intentos,
    obtener_detalle,
    refresh_cdr,
    refresh_pendientes,
)
from app.services.paquetes import TAMANO_MAXIMO_PAQUETE, importar_paquete
from app.services.results import (
    ComprobanteDetalleOut,
    ComprobanteOut,
    ComprobantesPage,
    EnviosResponse,
    PaqueteOut,
    RefreshCdrOut,
    RefreshLoteOut,
)

router = APIRouter(tags=["comprobantes"])


class FiltrosComprobantes(BaseModel):
    """Filtros del listado de comprobantes (query params validados)."""

    estado: EstadoComprobante | None = Field(
        default=None, description="Estado del frontend (pendiente, aprobado, ...)"
    )
    origen: OrigenComprobante | None = Field(
        default=None, description="emitido (ventas) o recibido (compras)"
    )
    fecha_desde: date | None = Field(default=None, description="Fecha de emisión mínima")
    fecha_hasta: date | None = Field(default=None, description="Fecha de emisión máxima")
    tipo_documento: str | None = Field(
        default=None, pattern=r"^[0-9]{2}$", description="01 factura, 03 boleta, 07/08 notas"
    )
    serie: str | None = Field(default=None, pattern=r"^[A-Z0-9]{1,4}$", description="Serie")
    q: str | None = Field(
        default=None,
        min_length=2,
        max_length=50,
        description="Búsqueda parcial por nombre de archivo",
    )
    limit: int = Field(default=50, ge=1, le=MAX_COMPROBANTES_PAGINA)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _rango_de_fechas(self) -> FiltrosComprobantes:
        if self.fecha_desde and self.fecha_hasta and self.fecha_desde > self.fecha_hasta:
            raise ValueError("fecha_desde no puede ser posterior a fecha_hasta")
        return self


@router.get(
    "/companies/{ruc}/comprobantes",
    response_model=ComprobantesPage,
    summary="Lista los comprobantes de la empresa",
)
async def get_comprobantes(
    access: CompanyAccessDep,
    uow: UowDep,
    filtros: Annotated[FiltrosComprobantes, Query()],
) -> ComprobantesPage:
    """Devuelve una página de comprobantes; cualquier miembro (incluido `lector`)."""
    return await list_comprobantes(
        uow=uow,
        access=access,
        estado=filtros.estado,
        origen=filtros.origen,
        fecha_desde=filtros.fecha_desde,
        fecha_hasta=filtros.fecha_hasta,
        tipo_documento=filtros.tipo_documento,
        serie=filtros.serie,
        q=filtros.q,
        limit=filtros.limit,
        offset=filtros.offset,
    )


@router.post(
    "/companies/{ruc}/comprobantes/refresh",
    response_model=RefreshLoteOut,
    summary="Refresca el estado en SUNAT de los comprobantes pendientes",
)
async def post_refresh_pendientes(
    access: AcessoOperativo,
    user: CurrentUserDep,
    uow: UowDep,
    settings: SettingsDep,
    secrets_provider: SecretsProviderDep,
    client_factory: SunatClientFactoryDep,
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_LOTE_REFRESCO, description="Máximo de comprobantes a consultar"),
    ] = 25,
) -> RefreshLoteOut:
    """Sincroniza de una vez los pendientes de la empresa (admin/emisor).

    Un fallo de SUNAT detiene el lote y lo reporta en `detenido_por`.
    """
    return await refresh_pendientes(
        uow=uow,
        settings=settings,
        secrets_provider=secrets_provider,
        client_factory=client_factory,
        user=user,
        access=access,
        limit=limit,
    )


@router.post(
    "/companies/{ruc}/comprobantes/import",
    response_model=ComprobanteOut,
    status_code=status.HTTP_201_CREATED,
    summary="Importa un comprobante desde su XML UBL",
)
async def post_importar_comprobante(
    access: AcessoOperativo,
    user: CurrentUserDep,
    uow: UowDep,
    settings: SettingsDep,
    archivo: Annotated[UploadFile, File(description="XML UBL 2.1 del comprobante")],
    origen: Annotated[
        OrigenComprobante,
        Form(description="emitido (venta propia) o recibido (compra a un proveedor)"),
    ] = OrigenComprobante.emitido,
) -> ComprobanteOut:
    """Importa un comprobante UBL (admin/emisor). Un duplicado responde 409.

    Con `origen=recibido` se importa una compra: el emisor es el proveedor y el
    adquirente del XML debe ser la empresa.
    """
    contenido = await leer_con_limite(archivo, TAMANO_MAXIMO_XML)
    return await import_comprobante_xml(
        uow=uow,
        settings=settings,
        user=user,
        access=access,
        xml=contenido,
        origen=origen,
    )


@router.post(
    "/companies/{ruc}/comprobantes/import-paquete",
    response_model=PaqueteOut,
    summary="Importa un paquete ZIP con varios XML (y sus PDF)",
)
async def post_importar_paquete(
    access: AcessoOperativo,
    user: CurrentUserDep,
    uow: UowDep,
    settings: SettingsDep,
    paquete: Annotated[UploadFile, File(description="ZIP con los XML y sus PDF")],
    origen: Annotated[
        OrigenComprobante,
        Form(description="emitido (ventas propias) o recibido (compras)"),
    ] = OrigenComprobante.emitido,
) -> PaqueteOut:
    """Carga masiva: cada XML del ZIP se importa y su PDF se archiva (admin/emisor).

    Un XML con error (duplicado, inválido, de otro RUC) no detiene el resto: se
    informa en `items` con su código de error.
    """
    contenido = await leer_con_limite(paquete, TAMANO_MAXIMO_PAQUETE)
    return await importar_paquete(
        uow=uow,
        settings=settings,
        user=user,
        access=access,
        paquete=contenido,
        origen=origen,
    )


@router.get(
    "/comprobantes/{comprobante_id}",
    response_model=ComprobanteDetalleOut,
    summary="Detalle de un comprobante con sus ítems",
)
async def get_comprobante(
    user: CurrentUserDep,
    uow: UowDep,
    comprobante_id: UUID = Path(description="Identificador del comprobante"),
) -> ComprobanteDetalleOut:
    """Comprobante completo (cabecera + líneas del detalle); cualquier miembro."""
    return await obtener_detalle(uow=uow, user=user, comprobante_id=comprobante_id)


class ActualizarSapIn(BaseModel):
    """Campos de gestión SAP editables; lo que no se envía no se modifica."""

    check_sap: bool | None = Field(default=None, description="Marca de conciliación con SAP")
    n_sap: str | None = Field(
        default=None, max_length=50, description="N° SAP; null o vacío lo limpia"
    )

    @model_validator(mode="after")
    def _al_menos_un_campo(self) -> ActualizarSapIn:
        if not self.model_fields_set & {"check_sap", "n_sap"}:
            raise ValueError("Envíe check_sap y/o n_sap")
        if "check_sap" in self.model_fields_set and self.check_sap is None:
            raise ValueError("check_sap debe ser true o false")
        return self


@router.patch(
    "/comprobantes/{comprobante_id}",
    response_model=ComprobanteOut,
    summary="Actualiza el Check SAP / N° SAP del comprobante",
)
async def patch_comprobante(
    user: CurrentUserDep,
    uow: UowDep,
    cuerpo: ActualizarSapIn,
    comprobante_id: UUID = Path(description="Identificador del comprobante"),
) -> ComprobanteOut:
    """Persiste la marca SAP (admin/emisor); el `lector` recibe 403."""
    n_sap = (cuerpo.n_sap or "").strip() or None
    return await actualizar_sap(
        uow=uow,
        user=user,
        comprobante_id=comprobante_id,
        check_sap=cuerpo.check_sap,
        n_sap=n_sap,
        cambiar_n_sap="n_sap" in cuerpo.model_fields_set,
    )


@router.get(
    "/comprobantes/{comprobante_id}/pdf",
    summary="Descarga la representación impresa del comprobante en PDF",
)
async def get_comprobante_pdf(
    user: CurrentUserDep,
    uow: UowDep,
    comprobante_id: UUID = Path(description="Identificador del comprobante"),
) -> Response:
    """Genera el PDF bajo demanda desde los datos registrados; cualquier miembro.

    No depende del bucket ni de SUNAT: el PDF se construye con la cabecera y las
    líneas ya guardadas en la base.
    """
    contenido, nombre = await generar_pdf_comprobante(
        uow=uow, user=user, comprobante_id=comprobante_id
    )
    seguro = nombre.replace('"', "").replace("\\", "")
    return Response(
        content=contenido,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{seguro}"'},
    )


@router.get(
    "/comprobantes/{comprobante_id}/envios",
    response_model=EnviosResponse,
    summary="Bitácora de intentos contra SUNAT del comprobante",
)
async def get_intentos(
    user: CurrentUserDep,
    uow: UowDep,
    comprobante_id: UUID = Path(description="Identificador del comprobante"),
    limit: Annotated[int, Query(ge=1, le=MAX_INTENTOS_PAGINA)] = 20,
) -> EnviosResponse:
    """Trazabilidad por comprobante: qué se consultó, cuándo y con qué resultado."""
    return await list_intentos(
        uow=uow, user=user, comprobante_id=comprobante_id, limit=limit
    )


@router.post(
    "/comprobantes/{comprobante_id}/refresh-cdr",
    response_model=RefreshCdrOut,
    summary="Consulta SUNAT y actualiza el estado del comprobante",
)
async def post_refresh_cdr(
    user: CurrentUserDep,
    uow: UowDep,
    settings: SettingsDep,
    secrets_provider: SecretsProviderDep,
    client_factory: SunatClientFactoryDep,
    comprobante_id: UUID = Path(description="Identificador del comprobante"),
) -> RefreshCdrOut:
    """Consulta el estado/CDR en SUNAT, actualiza `comprobantes` y registra el intento.

    - Si SUNAT todavía no conoce el comprobante, el estado queda en Pendiente.
    - Si SUNAT no responde, se devuelve 503 y el intento queda registrado.
    - Una consulta nunca revierte el estado Anulado.
    """
    return await refresh_cdr(
        uow=uow,
        settings=settings,
        secrets_provider=secrets_provider,
        client_factory=client_factory,
        user=user,
        comprobante_id=comprobante_id,
    )
