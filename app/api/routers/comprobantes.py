"""Endpoints de comprobantes: listado, importación de XML y refresco del CDR."""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, Path, Query, UploadFile, status
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
from app.core.enums import (
    CampoOrden,
    DireccionOrden,
    EstadoComprobante,
    OrigenComprobante,
    RespaldoFiltro,
)
from app.core.errors import FileTooLargeError
from app.db.repositories.base import FiltrosListado
from app.services.carpeta_local import abrir_carpeta
from app.services.cdr_paquete import TAMANO_MAXIMO_PAQUETE as TAMANO_MAXIMO_CDR
from app.services.cdr_paquete import importar_cdrs
from app.services.comprobantes import (
    MAX_COMPROBANTES_PAGINA,
    MAX_INTENTOS_PAGINA,
    MAX_LOTE_REFRESCO,
    TAMANO_MAXIMO_XML,
    actualizar_sap,
    import_comprobante_xml,
    list_comprobantes,
    list_intentos,
    obtener_detalle,
    refresh_cdr,
    refresh_pendientes,
    resumen_comprobantes,
)
from app.services.paquetes import TAMANO_MAXIMO_PAQUETE, importar_paquete
from app.services.proveedores_paquete import (
    MAX_BYTES_GRUPO,
    MAX_BYTES_POR_ENTRADA,
    importar_archivos_proveedores,
    importar_paquete_proveedores,
)
from app.services.results import (
    CdrPaqueteOut,
    ComprobanteDetalleOut,
    ComprobanteOut,
    ComprobantesPage,
    EnviosResponse,
    PaqueteOut,
    RefreshCdrOut,
    RefreshLoteOut,
    ResumenOut,
    SapImportOut,
)
from app.services.sap import MAX_FILAS_SAP, importar_sap

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
        description=(
            "Búsqueda parcial por N° de documento, cliente, RUC, orden de compra o archivo"
        ),
    )
    n_sap: str | None = Field(
        default=None, min_length=1, max_length=50, description="N° SAP (contiene)"
    )
    vencimiento_desde: date | None = Field(default=None, description="Vencimiento mínimo")
    vencimiento_hasta: date | None = Field(default=None, description="Vencimiento máximo")
    moneda: str | None = Field(default=None, pattern=r"^[A-Z]{3}$", description="PEN, USD…")
    respaldo: RespaldoFiltro | None = Field(
        default=None, description="Cobertura de respaldo: sin_xml, sin_cdr o completo"
    )
    orden: CampoOrden = Field(default=CampoOrden.fecha, description="Columna de orden")
    direccion: DireccionOrden = Field(default=DireccionOrden.desc)
    limit: int = Field(default=50, ge=1, le=MAX_COMPROBANTES_PAGINA)
    offset: int = Field(default=0, ge=0)
    contar: bool = Field(
        default=True,
        description="Incluye `total`; pídelo solo en la 1.ª página o al cambiar de filtro",
    )

    @model_validator(mode="after")
    def _rango_de_fechas(self) -> FiltrosComprobantes:
        if self.fecha_desde and self.fecha_hasta and self.fecha_desde > self.fecha_hasta:
            raise ValueError("fecha_desde no puede ser posterior a fecha_hasta")
        if (
            self.vencimiento_desde
            and self.vencimiento_hasta
            and self.vencimiento_desde > self.vencimiento_hasta
        ):
            raise ValueError("vencimiento_desde no puede ser posterior a vencimiento_hasta")
        return self

    def a_filtros(self) -> FiltrosListado:
        return FiltrosListado(
            estado=self.estado,
            origen=self.origen,
            fecha_desde=self.fecha_desde,
            fecha_hasta=self.fecha_hasta,
            tipo_documento=self.tipo_documento,
            serie=self.serie,
            q=self.q,
            n_sap=self.n_sap,
            vencimiento_desde=self.vencimiento_desde,
            vencimiento_hasta=self.vencimiento_hasta,
            moneda=self.moneda,
            respaldo=self.respaldo,
            orden=self.orden,
            direccion=self.direccion,
        )


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
        filtros=filtros.a_filtros(),
        limit=filtros.limit,
        offset=filtros.offset,
        contar=filtros.contar,
    )


@router.get(
    "/companies/{ruc}/comprobantes/resumen",
    response_model=ResumenOut,
    summary="Indicadores del conjunto filtrado (estados, facturado neto, respaldo)",
)
async def get_resumen_comprobantes(
    access: CompanyAccessDep,
    uow: UowDep,
    filtros: Annotated[FiltrosComprobantes, Query()],
) -> ResumenOut:
    """Agregados calculados en la base: no dependen de cuántas páginas cargó el cliente."""
    return await resumen_comprobantes(uow=uow, access=access, filtros=filtros.a_filtros())


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


@router.post(
    "/companies/{ruc}/comprobantes/import-proveedores",
    response_model=PaqueteOut,
    summary="Importa compras desde un ZIP por carpetas (XML + PDF + CDR de cada proveedor)",
)
async def post_importar_proveedores(
    access: AcessoOperativo,
    user: CurrentUserDep,
    uow: UowDep,
    settings: SettingsDep,
    paquete: Annotated[UploadFile, File(description="ZIP con una carpeta por compra")],
) -> PaqueteOut:
    """Registra compras que llegaron por correo y SIRE no refleja (admin/emisor).

    Cada carpeta reúne el XML, los PDF y el CDR de una compra (`RUC/SERIE-NÚMERO/…`); también se
    acepta un ZIP plano. Si SIRE ya trae la compra, el XML la completa; lo ya registrado no se
    duplica pero sí recibe el PDF o el CDR que le falte.
    """
    contenido = await leer_con_limite(paquete, TAMANO_MAXIMO_PAQUETE)
    return await importar_paquete_proveedores(
        uow=uow, settings=settings, user=user, access=access, paquete=contenido
    )


@router.post(
    "/companies/{ruc}/comprobantes/import-proveedores/archivos",
    response_model=PaqueteOut,
    summary="Importa UNA compra de proveedor (su XML, PDF y CDR) enviada como archivos sueltos",
)
async def post_importar_proveedores_archivos(
    access: AcessoOperativo,
    user: CurrentUserDep,
    uow: UowDep,
    settings: SettingsDep,
    archivos: Annotated[list[UploadFile], File(description="XML, PDF y CDR de una compra")],
) -> PaqueteOut:
    """Igual que `import-proveedores`, pero con los archivos de una sola compra (admin/emisor).

    Existe para que el portal importe de a una compra y muestre el avance en tiempo real: cada
    llamada responde un resultado por archivo (`tipo`, `situacion`, `code`, `mensaje`). Un PDF
    sin XML se une a una compra ya registrada solo si su texto da el RUC y la serie-número exactos.
    """
    recibidos: list[tuple[str, bytes]] = []
    total = 0
    for archivo in archivos:
        contenido = await leer_con_limite(archivo, MAX_BYTES_POR_ENTRADA)
        total += len(contenido)
        if total > MAX_BYTES_GRUPO:
            raise FileTooLargeError(details={"maximo_bytes": MAX_BYTES_GRUPO})
        recibidos.append((archivo.filename or "archivo", contenido))
    return await importar_archivos_proveedores(
        uow=uow, settings=settings, user=user, access=access, archivos=recibidos
    )


@router.post(
    "/companies/{ruc}/comprobantes/import-cdr",
    response_model=CdrPaqueteOut,
    summary="Carga masiva de CDR (constancias de recepción) desde un ZIP",
)
async def post_importar_cdrs(
    access: AcessoOperativo,
    user: CurrentUserDep,
    uow: UowDep,
    settings: SettingsDep,
    paquete: Annotated[
        UploadFile, File(description="ZIP con los CDR R-{RUC}-{tipo}-{serie}-{numero}")
    ],
) -> CdrPaqueteOut:
    """Asocia cada CDR del ZIP a su comprobante emitido por el nombre (admin/emisor).

    Es la vía para los comprobantes que emite otro sistema (OSE/PSE/SAP): SUNAT no
    devuelve su CDR por consulta. Cada CDR se archiva en R2 con su hash y actualiza
    el estado; los que ya tienen CDR no se pisan.
    """
    contenido = await leer_con_limite(paquete, TAMANO_MAXIMO_CDR)
    return await importar_cdrs(
        uow=uow, settings=settings, user=user, access=access, paquete=contenido
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


class CarpetaLocalIn(BaseModel):
    partes: list[str] = Field(
        min_length=4,
        max_length=4,
        description="Año, mes, RUC de la contraparte y SERIE-NÚMERO (la carpeta del comprobante)",
    )


class CarpetaLocalOut(BaseModel):
    ruta: str = Field(description="Ruta completa de la carpeta abierta en la PC")


@router.post(
    "/carpeta-local/abrir",
    response_model=CarpetaLocalOut,
    summary="Abre en el Explorador la carpeta local de un comprobante (solo backend en tu PC)",
)
async def post_abrir_carpeta(
    _user: CurrentUserDep, settings: SettingsDep, datos: CarpetaLocalIn
) -> CarpetaLocalOut:
    """`Descargas/AAAA/MM/RUC/SERIE-NÚMERO`. 409 si el backend no corre en la PC del usuario.

    No usa la base de datos: solo valida el token y abre una carpeta existente bajo la base.
    """
    return CarpetaLocalOut(ruta=abrir_carpeta(settings=settings, partes=datos.partes))


class ActualizarSapIn(BaseModel):
    """N° SAP de un comprobante."""

    n_sap: str | None = Field(
        default=None, max_length=50, description="N° SAP; null o vacío lo limpia"
    )

    @model_validator(mode="after")
    def _exige_n_sap(self) -> ActualizarSapIn:
        if "n_sap" not in self.model_fields_set:
            raise ValueError("Envíe n_sap")
        return self


class FilaSapIn(BaseModel):
    """Una fila del Excel de N° SAP, tal como viene (el servidor la normaliza)."""

    documento: str | None = Field(
        default=None, max_length=40, description="Serie-número, p. ej. F001-123"
    )
    ruc: str | None = Field(default=None, max_length=20, description="RUC (opcional)")
    n_sap: str | None = Field(default=None, max_length=100, description="N° SAP")


class ImportarSapIn(BaseModel):
    filas: list[FilaSapIn] = Field(max_length=MAX_FILAS_SAP)


@router.patch(
    "/comprobantes/{comprobante_id}",
    response_model=ComprobanteOut,
    summary="Actualiza el N° SAP del comprobante",
)
async def patch_comprobante(
    user: CurrentUserDep,
    uow: UowDep,
    cuerpo: ActualizarSapIn,
    comprobante_id: UUID = Path(description="Identificador del comprobante"),
) -> ComprobanteOut:
    """Persiste el N° SAP (admin/emisor); el `lector` recibe 403."""
    n_sap = (cuerpo.n_sap or "").strip() or None
    return await actualizar_sap(uow=uow, user=user, comprobante_id=comprobante_id, n_sap=n_sap)


@router.post(
    "/companies/{ruc}/comprobantes/import-sap",
    response_model=SapImportOut,
    summary="Importa N° SAP en lote (documento y N° SAP por fila)",
)
async def post_importar_sap(
    access: AcessoOperativo, uow: UowDep, cuerpo: ImportarSapIn
) -> SapImportOut:
    """Asigna el N° SAP a los comprobantes ya registrados (admin/emisor).

    Cada fila trae el documento (serie-número) y, opcional, el RUC. Lo que no está en el
    portal o no se puede leer se omite y se cuenta: nunca detiene la importación.
    """
    return await importar_sap(
        uow=uow,
        access=access,
        filas=[(fila.documento, fila.ruc, fila.n_sap) for fila in cuerpo.filas],
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
