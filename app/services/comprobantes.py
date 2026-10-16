"""Servicio `refresh-cdr`: consulta SUNAT, actualiza el estado y registra el intento."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

from app.core.concurrency import run_blocking
from app.core.config import Settings
from app.core.enums import (
    ROLES_OPERATIVOS,
    EstadoComprobante,
    OrigenComprobante,
    SunatAmbiente,
    TipoArchivo,
    TipoOperacionSunat,
)
from app.core.errors import (
    AppError,
    ConflictError,
    FileTooLargeError,
    InvalidDocumentError,
    NotFoundError,
    SunatError,
)
from app.core.logging import get_logger
from app.core.redaction import redact
from app.core.security import AuthenticatedUser
from app.db.models import (
    Company,
    CompanyMember,
    Comprobante,
    ComprobanteItem,
    ComprobanteItemRegistro,
    ComprobanteRegistro,
    SunatEnvio,
    SunatEnvioRegistro,
)
from app.db.repositories.base import UnitOfWork
from app.integrations.pdf import SunatPdfRenderer
from app.secrets.base import SecretsProvider
from app.services.companies import require_membership
from app.services.results import (
    CdrOut,
    ComprobanteDetalleOut,
    ComprobanteOut,
    ComprobantesPage,
    EnviosResponse,
    IntentoOut,
    ItemOut,
    RefreshCdrOut,
    RefreshLoteItemOut,
    RefreshLoteOut,
)
from app.services.store import guardar_archivo_sin_fallar
from app.services.sunat_status import SunatClientFactory, registrar_intento_seguro
from app.sunat.models import ResultadoConsulta, TramasSOAP
from app.sunat.states import map_cdr_a_estado, map_codigo_a_estado
from app.sunat.ubl import ComprobanteUbl, ItemUbl, parse_comprobante_ubl

MAX_COMPROBANTES_PAGINA = 100
MAX_INTENTOS_PAGINA = 50
MAX_LOTE_REFRESCO = 50
TAMANO_MAXIMO_XML = 5 * 1024 * 1024  # 5 MB, de sobra para un XML UBL

# Errores que detienen un refresco masivo: insistir solo multiplica el timeout.
_CODIGOS_QUE_DETIENEN_EL_LOTE = frozenset(
    {
        "sunat_auth_error",
        "sunat_unavailable",
        "sunat_consult_unavailable",
        "credentials_not_configured",
    }
)

logger = get_logger(__name__)


async def refresh_cdr(
    *,
    uow: UnitOfWork,
    settings: Settings,
    secrets_provider: SecretsProvider,
    client_factory: SunatClientFactory,
    user: AuthenticatedUser,
    comprobante_id: UUID,
) -> RefreshCdrOut:
    """Consulta el estado/CDR de un comprobante ya emitido y sincroniza la base."""
    comprobante = await uow.comprobantes.get(comprobante_id)
    if comprobante is None:
        # Bajo RLS los comprobantes de otras empresas no son visibles: 404 evita
        # filtrar la existencia del recurso.
        raise NotFoundError("El comprobante no existe o no pertenece a sus empresas")

    await require_membership(uow, user, comprobante.company_id, roles=ROLES_OPERATIVOS)

    if comprobante.es_recibido:
        # El CDR de una compra es del proveedor: la empresa no puede pedirlo.
        raise ConflictError(
            "El estado ante SUNAT de una compra lo informa el proveedor; "
            "este comprobante solo se archiva en el portal"
        )

    env = settings.effective_sunat_env(comprobante.sunat_ambiente)
    credentials = secrets_provider.get_company_credentials(comprobante.ruc_emisor)

    try:
        client = client_factory(credentials, env)
        # La llamada a SUNAT es síncrona (zeep): se ejecuta en un hilo para no
        # congelar el event loop durante el timeout.
        resultado = await run_blocking(
            client.get_status_cdr,
            tipo_comprobante=comprobante.tipo_documento,
            serie=comprobante.serie,
            numero=comprobante.numero,
        )
    except SunatError as exc:
        return await _manejar_error(
            uow=uow, comprobante=comprobante, env=env, exc=exc, usuario=user
        )

    propuesto = (
        map_cdr_a_estado(resultado.cdr)
        if resultado.cdr is not None
        else map_codigo_a_estado(resultado.estado.codigo, resultado.estado.mensaje)
    )
    estado_nuevo = _siguiente_estado(comprobante.estado, propuesto)

    actualizado = await uow.comprobantes.update_sunat_state(
        comprobante.id,
        estado=estado_nuevo,
        cdr_codigo=resultado.cdr.codigo if resultado.cdr else None,
        cdr_descripcion=resultado.cdr.descripcion if resultado.cdr else None,
        cdr_notas=resultado.cdr.notas if resultado.cdr else [],
    )

    intento = await _registrar(
        uow=uow,
        comprobante=comprobante,
        env=env,
        tramas=resultado.tramas,
        exito=True,
        codigo=resultado.cdr.codigo if resultado.cdr else resultado.estado.codigo,
        mensaje=resultado.cdr.descripcion if resultado.cdr else resultado.estado.mensaje,
        notas=resultado.cdr.notas if resultado.cdr else [],
        error_tipo="cdr_parse_error" if resultado.error_cdr else None,
        usuario=user,
    )
    await _guardar_cdr(
        uow=uow, settings=settings, comprobante=comprobante, resultado=resultado
    )

    return RefreshCdrOut(
        comprobante_id=comprobante.id,
        numero=comprobante.nombre_archivo or comprobante.numero,
        estado_anterior=comprobante.estado,
        estado_nuevo=actualizado.estado,
        estado_label=actualizado.estado.etiqueta,
        cambio_estado=actualizado.estado is not comprobante.estado,
        cdr=_cdr_out(resultado),
        mensaje=_mensaje(resultado, comprobante),
        sunat_env=env,
        intento=_intento_out(intento),
        consultado_at=datetime.now(tz=UTC),
    )


async def _manejar_error(
    *,
    uow: UnitOfWork,
    comprobante: Comprobante,
    env: SunatAmbiente,
    exc: SunatError,
    usuario: AuthenticatedUser,
) -> RefreshCdrOut:
    """Un comprobante que SUNAT aún no conoce deja el estado en Pendiente, no falla."""
    tramas: TramasSOAP | None = exc.details.get("tramas")
    intento = await _registrar(
        uow=uow,
        comprobante=comprobante,
        env=env,
        tramas=tramas,
        exito=False,
        codigo=exc.details.get("codigo"),
        mensaje=exc.message,
        notas=[],
        error_tipo=exc.code,
        usuario=usuario,
    )

    if not exc.details.get("no_disponible"):
        if intento is not None:
            exc.details["intento_id"] = intento.id
        raise exc

    estado_nuevo = _siguiente_estado(comprobante.estado, EstadoComprobante.pendiente)
    actualizado = await uow.comprobantes.update_sunat_state(
        comprobante.id,
        estado=estado_nuevo,
        cdr_codigo=None,
        cdr_descripcion=None,
        cdr_notas=[],
    )

    return RefreshCdrOut(
        comprobante_id=comprobante.id,
        numero=comprobante.nombre_archivo or comprobante.numero,
        estado_anterior=comprobante.estado,
        estado_nuevo=actualizado.estado,
        estado_label=actualizado.estado.etiqueta,
        cambio_estado=actualizado.estado is not comprobante.estado,
        cdr=None,
        mensaje=redact(exc.message),
        sunat_env=env,
        intento=_intento_out(intento),
        consultado_at=datetime.now(tz=UTC),
    )


async def _guardar_cdr(
    *,
    uow: UnitOfWork,
    settings: Settings,
    comprobante: Comprobante,
    resultado: ResultadoConsulta,
) -> None:
    """Archiva el ZIP del CDR una sola vez por comprobante (best-effort)."""
    cdr = resultado.cdr
    if cdr is None or not cdr.zip_bytes:
        return

    existentes = await uow.archivos.list_for_comprobante(comprobante.id)
    if any(archivo.tipo is TipoArchivo.cdr for archivo in existentes):
        return

    nombre = f"{comprobante.nombre_archivo or comprobante.numero}-CDR.zip"
    await guardar_archivo_sin_fallar(
        uow=uow,
        settings=settings,
        comprobante_id=comprobante.id,
        tipo=TipoArchivo.cdr,
        file_bytes=cdr.zip_bytes,
        filename=nombre,
    )


def _siguiente_estado(actual: EstadoComprobante, propuesto: EstadoComprobante) -> EstadoComprobante:
    """Una consulta de estado nunca revierte una anulación ni una aprobación.

    Aprobado es definitivo: SUNAT ya lo aceptó (o lo lista en su RVIE). Una consulta
    que no puede resolverlo —por ejemplo `0013`, comprobante emitido por otro
    sistema— no debe degradarlo a Pendiente ni a Rechazado.
    """
    if actual is EstadoComprobante.anulado and propuesto is not EstadoComprobante.anulado:
        logger.info("Se conserva el estado Anulado del comprobante consultado")
        return actual
    if actual is EstadoComprobante.aprobado and propuesto in (
        EstadoComprobante.pendiente,
        EstadoComprobante.rechazado,
    ):
        logger.info("Se conserva el estado Aprobado del comprobante consultado")
        return actual
    return propuesto


def _cdr_out(resultado: ResultadoConsulta) -> CdrOut | None:
    if resultado.cdr is None:
        return None
    return CdrOut(
        codigo=resultado.cdr.codigo,
        descripcion=resultado.cdr.descripcion,
        notas=resultado.cdr.notas,
        archivo=resultado.cdr.archivo,
        identificador=resultado.cdr.identificador,
        fecha_respuesta=resultado.cdr.fecha_respuesta,
    )


def _mensaje(resultado: ResultadoConsulta, comprobante: Comprobante) -> str:
    if resultado.cdr is not None:
        return f"CDR recibido: {resultado.cdr.descripcion}".strip()
    if resultado.error_cdr:
        return f"SUNAT devolvió un CDR que no se pudo interpretar: {resultado.error_cdr}"
    if resultado.estado.codigo is not None:
        detalle = f" ({resultado.estado.mensaje})" if resultado.estado.mensaje else ""
        return f"SUNAT devolvió el código {resultado.estado.codigo}{detalle} sin CDR adjunto"
    return "SUNAT no devolvió información de estado para el comprobante"


async def _registrar(
    *,
    uow: UnitOfWork,
    comprobante: Comprobante,
    env: SunatAmbiente,
    tramas: TramasSOAP | None,
    exito: bool,
    codigo: str | None,
    mensaje: str | None,
    notas: list[str],
    error_tipo: str | None,
    usuario: AuthenticatedUser,
) -> SunatEnvio | None:
    registro = SunatEnvioRegistro(
        company_id=comprobante.company_id,
        comprobante_id=comprobante.id,
        tipo_operacion=TipoOperacionSunat.consulta_cdr,
        sunat_ambiente=env,
        endpoint=tramas.endpoint if tramas else None,
        metodo=tramas.metodo if tramas else None,
        request_xml=tramas.request_xml if tramas else None,
        response_xml=tramas.response_xml if tramas else None,
        http_status=tramas.http_status if tramas else None,
        codigo_respuesta=codigo,
        mensaje_respuesta=redact(mensaje) if mensaje else None,
        notas=notas,
        exito=exito,
        error_tipo=error_tipo,
        duracion_ms=tramas.duracion_ms if tramas else None,
    )
    return await registrar_intento_seguro(uow, registro, usuario)


def _intento_out(intento: SunatEnvio | None) -> IntentoOut | None:
    if intento is None:
        return None
    return IntentoOut(
        id=intento.id,
        tipo_operacion=intento.tipo_operacion,
        exito=intento.exito,
        http_status=intento.http_status,
        codigo=intento.codigo_respuesta,
        mensaje=intento.mensaje_respuesta,
        error_tipo=intento.error_tipo,
        duracion_ms=intento.duracion_ms,
        created_at=intento.created_at,
    )


# --------------------------------------------------------------------------- #
# Listado e importación (fase 2)
# --------------------------------------------------------------------------- #
def to_comprobante_out(comprobante: Comprobante) -> ComprobanteOut:
    """Proyección del comprobante para el frontend (con `estado_label`)."""
    return ComprobanteOut(
        id=comprobante.id,
        company_id=comprobante.company_id,
        origen=comprobante.origen,
        origen_label=comprobante.origen.etiqueta,
        ruc_emisor=comprobante.ruc_emisor,
        ruc_receptor=comprobante.ruc_receptor,
        razon_social_emisor=comprobante.razon_social_emisor,
        razon_social_receptor=comprobante.razon_social_receptor,
        tipo_documento=comprobante.tipo_documento,
        serie=comprobante.serie,
        correlativo=comprobante.correlativo,
        numero=comprobante.numero,
        nombre_archivo=comprobante.nombre_archivo,
        fecha_emision=comprobante.fecha_emision,
        fecha_vencimiento=comprobante.fecha_vencimiento,
        forma_pago=comprobante.forma_pago,
        fuente=comprobante.fuente,
        moneda=comprobante.moneda,
        monto_total=comprobante.monto_total,
        estado=comprobante.estado,
        estado_label=comprobante.estado.etiqueta,
        sunat_ambiente=comprobante.sunat_ambiente,
        n_sap=comprobante.n_sap,
        orden_compra=comprobante.orden_compra,
        check_sap=comprobante.check_sap,
        cdr_codigo=comprobante.cdr_codigo,
        cdr_descripcion=comprobante.cdr_descripcion,
        created_at=comprobante.created_at,
        updated_at=comprobante.updated_at,
    )


async def list_comprobantes(
    *,
    uow: UnitOfWork,
    access: CompanyMember,
    estado: EstadoComprobante | None = None,
    origen: OrigenComprobante | None = None,
    fecha_desde: date | None = None,
    fecha_hasta: date | None = None,
    tipo_documento: str | None = None,
    serie: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> ComprobantesPage:
    """Lista paginada y filtrable de los comprobantes de la empresa (RLS ya filtra)."""
    limite = max(1, min(limit, MAX_COMPROBANTES_PAGINA))
    desplazamiento = max(0, offset)
    # Se pide una fila extra para saber si hay más sin un count aparte.
    filas = await uow.comprobantes.list_for_company(
        access.company_id,
        estado=estado,
        origen=origen,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        tipo_documento=tipo_documento,
        serie=serie,
        q=q,
        limit=limite + 1,
        offset=desplazamiento,
    )
    hay_mas = len(filas) > limite
    return ComprobantesPage(
        items=[to_comprobante_out(fila) for fila in filas[:limite]],
        limit=limite,
        offset=desplazamiento,
        has_more=hay_mas,
    )


def _validar_compra(dato: ComprobanteUbl, company: Company) -> None:
    """Comprueba que el XML importado como compra sea realmente de la empresa.

    Si el adquirente no tiene RUC (boleta a DNI o consumidor final) no hay nada
    que validar: se archiva bajo la responsabilidad de quien la sube.
    """
    if dato.ruc_emisor == company.ruc:
        raise InvalidDocumentError(
            "El XML fue emitido por esta empresa: impórtelo como comprobante emitido",
            details={"ruc_emisor": dato.ruc_emisor},
        )
    if dato.ruc_receptor is not None and dato.ruc_receptor != company.ruc:
        raise InvalidDocumentError(
            "El comprobante fue emitido a otro adquirente y no es una compra de esta empresa",
            details={"ruc_receptor": dato.ruc_receptor, "ruc_empresa": company.ruc},
        )


async def refresh_pendientes(
    *,
    uow: UnitOfWork,
    settings: Settings,
    secrets_provider: SecretsProvider,
    client_factory: SunatClientFactory,
    user: AuthenticatedUser,
    access: CompanyMember,
    limit: int = 25,
) -> RefreshLoteOut:
    """Refresca el estado en SUNAT de los comprobantes pendientes de la empresa.

    Un fallo de infraestructura (SUNAT caído o credenciales rechazadas) **detiene**
    el lote: insistir con el resto solo encadenaría timeouts. Un fallo puntual de
    un comprobante se registra y el lote continúa.
    """
    limite = max(1, min(limit, MAX_LOTE_REFRESCO))
    # Solo emitidas: las compras no tienen CDR nuestro que sincronizar.
    pendientes = await uow.comprobantes.list_for_company(
        access.company_id,
        estado=EstadoComprobante.pendiente,
        origen=OrigenComprobante.emitido,
        limit=limite,
    )

    items: list[RefreshLoteItemOut] = []
    fallidos = 0
    con_cambio = 0
    detenido_por: str | None = None

    for comprobante in pendientes:
        try:
            resultado = await refresh_cdr(
                uow=uow,
                settings=settings,
                secrets_provider=secrets_provider,
                client_factory=client_factory,
                user=user,
                comprobante_id=comprobante.id,
            )
        except AppError as exc:
            fallidos += 1
            items.append(
                RefreshLoteItemOut(
                    comprobante_id=comprobante.id,
                    numero=comprobante.nombre_archivo or comprobante.numero,
                    estado_anterior=comprobante.estado,
                    estado_nuevo=comprobante.estado,
                    estado_label=comprobante.estado.etiqueta,
                    cambio_estado=False,
                    ok=False,
                    mensaje=redact(exc.message),
                    error_code=exc.code,
                )
            )
            if exc.code in _CODIGOS_QUE_DETIENEN_EL_LOTE:
                detenido_por = exc.code
                break
            continue

        con_cambio += 1 if resultado.cambio_estado else 0
        items.append(
            RefreshLoteItemOut(
                comprobante_id=resultado.comprobante_id,
                numero=resultado.numero,
                estado_anterior=resultado.estado_anterior,
                estado_nuevo=resultado.estado_nuevo,
                estado_label=resultado.estado_label,
                cambio_estado=resultado.cambio_estado,
                cdr_codigo=resultado.cdr.codigo if resultado.cdr else None,
                mensaje=redact(resultado.mensaje) if resultado.mensaje else None,
            )
        )

    return RefreshLoteOut(
        ruc=access.company.ruc,
        total=len(pendientes),
        procesados=len(items),
        con_cambio=con_cambio,
        fallidos=fallidos,
        detenido_por=detenido_por,
        items=items,
        consultado_at=datetime.now(tz=UTC),
    )


async def list_intentos(
    *,
    uow: UnitOfWork,
    user: AuthenticatedUser,
    comprobante_id: UUID,
    limit: int = 20,
) -> EnviosResponse:
    """Bitácora de los intentos contra SUNAT del comprobante (incluido `lector`).

    Es la vista de trazabilidad para soporte: código, mensaje, latencia y error de
    cada consulta, sin las tramas SOAP.
    """
    comprobante = await uow.comprobantes.get(comprobante_id)
    if comprobante is None:
        raise NotFoundError("El comprobante no existe o no pertenece a sus empresas")

    await require_membership(uow, user, comprobante.company_id)

    limite = max(1, min(limit, MAX_INTENTOS_PAGINA))
    envios = await uow.sunat_envios.list_for_comprobante(comprobante_id, limit=limite)
    items = [intento for intento in map(_intento_out, envios) if intento is not None]
    return EnviosResponse(comprobante_id=comprobante_id, items=items, total=len(items))


async def import_comprobante_xml(
    *,
    uow: UnitOfWork,
    settings: Settings,
    user: AuthenticatedUser,
    access: CompanyMember,
    xml: bytes,
    origen: OrigenComprobante = OrigenComprobante.emitido,
) -> ComprobanteOut:
    """Da de alta un comprobante a partir de su XML UBL.

    * `origen=emitido` (por defecto): el RUC del emisor debe ser el de la empresa.
    * `origen=recibido`: es una compra; el emisor es el proveedor y el adquirente
      del XML debe ser la empresa.

    Un XML repetido se rechaza con 409 (restricción `comprobantes_unicos_por_ambiente`).
    """
    if not xml or not xml.strip():
        raise InvalidDocumentError("El archivo XML está vacío")
    if len(xml) > TAMANO_MAXIMO_XML:
        raise FileTooLargeError(
            details={"maximo_bytes": TAMANO_MAXIMO_XML, "recibido_bytes": len(xml)}
        )

    es_compra = origen is OrigenComprobante.recibido
    dato = parse_comprobante_ubl(
        xml, ruc_esperado=None if es_compra else access.company.ruc
    )
    if es_compra:
        _validar_compra(dato, access.company)

    registro = ComprobanteRegistro(
        company_id=access.company_id,
        origen=origen,
        ruc_emisor=dato.ruc_emisor,
        ruc_receptor=dato.ruc_receptor,
        receptor_doc=dato.receptor_doc,
        razon_social_emisor=dato.razon_social_emisor,
        razon_social_receptor=dato.razon_social_receptor,
        tipo_documento=dato.tipo_documento,
        serie=dato.serie,
        correlativo=dato.correlativo,
        numero=f"{dato.correlativo:08d}",
        nombre_archivo=dato.nombre_archivo,
        fecha_emision=dato.fecha_emision,
        fecha_vencimiento=dato.fecha_vencimiento,
        forma_pago=dato.forma_pago,
        moneda=dato.moneda,
        monto_total=dato.monto_total,
        estado=EstadoComprobante.pendiente,
        sunat_ambiente=settings.effective_sunat_env(access.company.sunat_ambiente),
        created_by=user.id,
    )
    # Un registro que vino de SIRE solo tiene cabecera: el XML lo completa (con
    # sus ítems y el archivo) en lugar de chocar con él como duplicado.
    existente = await uow.comprobantes.find_by_key(
        access.company_id,
        origen=origen,
        tipo_documento=registro.tipo_documento,
        serie=registro.serie,
        correlativo=registro.correlativo,
        sunat_ambiente=registro.sunat_ambiente,
    )
    if existente is not None and existente.fuente == "sire":
        creado = await uow.comprobantes.completar_desde_xml(existente.id, registro)
    else:
        creado = await uow.comprobantes.create(registro)
    logger.info(
        "Comprobante %s importado (%s-%s)", origen.value, creado.serie, creado.numero
    )
    # Las líneas del detalle se registran en la misma transacción de la
    # petición: si falla, no queda un comprobante sin su detalle a medias.
    items = _items_a_registros(creado.id, dato.items)
    if items:
        await uow.items.create_many(items)
    # El XML original se archiva junto al comprobante (si el bucket no está
    # configurado, la importación se completa igual).
    await guardar_archivo_sin_fallar(
        uow=uow,
        settings=settings,
        comprobante_id=creado.id,
        tipo=TipoArchivo.xml,
        file_bytes=xml,
        filename=f"{dato.nombre_archivo}.xml",
    )
    return to_comprobante_out(creado)


_MAX_ITEMS_EXPORTADOS = 1000


def _items_a_registros(
    comprobante_id: UUID, items: tuple[ItemUbl, ...]
) -> list[ComprobanteItemRegistro]:
    """Convierte las líneas del XML en filas para `comprobante_items`."""
    return [
        ComprobanteItemRegistro(
            comprobante_id=comprobante_id,
            item_nro=item.item_nro,
            descripcion=item.descripcion[:500],
            cantidad=item.cantidad,
            unidad_medida=item.unidad_medida,
            codigo=item.codigo,
            valor_unitario=item.valor_unitario,
            precio_unitario=item.precio_unitario,
            descuento=item.descuento,
            igv=item.igv,
            isc=item.isc,
            tip_afectacion_igv=item.tip_afectacion_igv,
            total=item.total,
        )
        for item in items[:_MAX_ITEMS_EXPORTADOS]
    ]


def to_item_out(item: ComprobanteItem) -> ItemOut:
    """Proyección de una línea del detalle para el frontend."""
    return ItemOut(
        item_nro=item.item_nro,
        descripcion=item.descripcion,
        cantidad=item.cantidad,
        unidad_medida=item.unidad_medida,
        codigo=item.codigo,
        valor_unitario=item.valor_unitario,
        precio_unitario=item.precio_unitario,
        descuento=item.descuento,
        igv=item.igv,
        isc=item.isc,
        tip_afectacion_igv=item.tip_afectacion_igv,
        total=item.total,
    )


async def obtener_detalle(
    *, uow: UnitOfWork, user: AuthenticatedUser, comprobante_id: UUID
) -> ComprobanteDetalleOut:
    """Comprobante con su detalle de ítems; cualquier miembro (incluido `lector`)."""
    comprobante = await uow.comprobantes.get(comprobante_id)
    if comprobante is None:
        # Bajo RLS los comprobantes de otras empresas no son visibles: 404 evita
        # filtrar la existencia del recurso.
        raise NotFoundError("El comprobante no existe o no pertenece a sus empresas")

    await require_membership(uow, user, comprobante.company_id)

    items = await uow.items.list_for_comprobante(comprobante_id)
    base = to_comprobante_out(comprobante)
    return ComprobanteDetalleOut(**base.model_dump(), items=[to_item_out(item) for item in items])


async def actualizar_sap(
    *,
    uow: UnitOfWork,
    user: AuthenticatedUser,
    comprobante_id: UUID,
    check_sap: bool | None,
    n_sap: str | None,
    cambiar_n_sap: bool,
) -> ComprobanteOut:
    """Persiste la marca de conciliación SAP (y opcionalmente el N° SAP).

    Solo admin/emisor: es un dato de gestión que el `lector` no debe alterar.
    Aplica tanto a ventas como a compras.
    """
    comprobante = await uow.comprobantes.get(comprobante_id)
    if comprobante is None:
        raise NotFoundError("El comprobante no existe o no pertenece a sus empresas")

    await require_membership(uow, user, comprobante.company_id, roles=ROLES_OPERATIVOS)

    actualizado = await uow.comprobantes.update_sap(
        comprobante_id,
        check_sap=check_sap,
        n_sap=n_sap,
        cambiar_n_sap=cambiar_n_sap,
    )
    return to_comprobante_out(actualizado)


async def generar_pdf_comprobante(
    *,
    uow: UnitOfWork,
    user: AuthenticatedUser,
    comprobante_id: UUID,
    settings: Settings | None = None,
) -> tuple[bytes, str]:
    """Representación impresa del comprobante en PDF.

    Genera el documento bajo demanda a partir de los datos registrados (no
    depende de SUNAT). Devuelve `(contenido, nombre_archivo)`; cualquier miembro
    (incluido `lector`) puede descargarlo.

    La primera vez que un admin/emisor lo genera se archiva en R2 como **copia
    informativa**: no se construye desde el XML firmado, así que no es la evidencia
    ante SUNAT (esa es el XML con su CDR). El `lector` no archiva nada (no tiene
    permiso de escritura) y un fallo de R2 no impide la descarga.
    """
    comprobante = await uow.comprobantes.get(comprobante_id)
    if comprobante is None:
        raise NotFoundError("El comprobante no existe o no pertenece a sus empresas")

    miembro = await require_membership(uow, user, comprobante.company_id)

    items = await uow.items.list_for_comprobante(comprobante_id)
    contenido = await SunatPdfRenderer().render(comprobante, items)
    nombre = f"{comprobante.nombre_archivo or comprobante.numero or 'comprobante'}.pdf"

    if settings is not None and miembro.rol in ROLES_OPERATIVOS:
        archivados = await uow.archivos.list_for_comprobante(comprobante_id)
        if not any(archivo.tipo is TipoArchivo.pdf for archivo in archivados):
            await guardar_archivo_sin_fallar(
                uow=uow,
                settings=settings,
                comprobante_id=comprobante_id,
                tipo=TipoArchivo.pdf,
                file_bytes=contenido,
                filename=nombre,
            )
    return contenido, nombre
