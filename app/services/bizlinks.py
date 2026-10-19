"""Recupera desde Bizlinks el XML firmado y el CDR de los comprobantes emitidos.

El SIRE solo trae los datos del comprobante; el XML y el CDR los conserva el OSE que
firma y declara (Bizlinks). Este servicio completa, para auditoría, los comprobantes
emitidos que aún no tienen alguno de los dos archivos:

* XML firmado → se valida contra el comprobante (RUC, tipo, serie y correlativo) y se
  archiva en R2. Si el comprobante vino de SIRE y no tiene ítems, se cargan del XML.
* CDR → se archiva como ZIP (tal cual lo entrega Bizlinks; si llega como XML suelto se
  envuelve en un ZIP sin tocar su contenido) y actualiza el estado ante SUNAT.

Las boletas no reciben CDR individual (lo recibe el resumen diario que las incluye), por
lo que de ellas solo se recupera el XML.

El origen de los archivos es un cliente con la interfaz `obtener_respaldos`: la API del
OSE (`canal=api`) o, como alternativa, el portal público de consulta anónima
(`canal=portal`). Para el portal se aplican límites que protegen a la empresa de que
Bizlinks vea una consulta masiva:

* tope de comprobantes por ventana de 24 h y por llamada;
* cortacircuito: tras un bloqueo (403, 429, CAPTCHA) o un cambio de la página, las
  consultas se pausan `bizlinks_portal_cooldown_horas`;
* un comprobante que no se encontró (o falló) no se vuelve a consultar en 24 h.

Cada consulta se anota en `bizlinks_consultas`. Una caída o un rechazo **detiene** el
lote; un fallo puntual de un comprobante se registra y se continúa. El bloqueo se
devuelve como resultado (no como excepción) para que su registro se confirme.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from app.core.concurrency import run_blocking
from app.core.config import Settings
from app.core.enums import OrigenComprobante, TipoArchivo
from app.core.errors import (
    AppError,
    BizlinksBlockedError,
    BizlinksPortalChangedError,
    InvalidDocumentError,
)
from app.core.logging import get_logger
from app.core.redaction import redact
from app.db.models import BizlinksConsultaRegistro, CompanyMember, Comprobante
from app.db.repositories.base import FiltrosListado, UnitOfWork
from app.secrets.base import SecretsProvider
from app.services.comprobantes import _items_a_registros as items_a_registros
from app.services.comprobantes import siguiente_estado
from app.services.results import BizlinksItemOut, BizlinksSyncOut
from app.services.store import guardar_archivo_sin_fallar
from app.sunat.bizlinks import RespaldosBizlinks
from app.sunat.cdr import parse_cdr_xml, parse_cdr_zip
from app.sunat.models import CdrInfo
from app.sunat.states import map_cdr_a_estado
from app.sunat.ubl import parse_comprobante_ubl

logger = get_logger(__name__)

LIMITE_POR_DEFECTO = 25
LIMITE_MAXIMO = 100
# Cuántos comprobantes se revisan buscando los que les falta algo, por llamada.
MAX_REVISADOS = 1000
_PAGINA = 100
VENTANA = timedelta(hours=24)

_CODIGOS_QUE_DETIENEN_EL_LOTE = frozenset(
    {
        "bizlinks_auth_error",
        "bizlinks_unavailable",
        "bizlinks_not_configured",
        "bizlinks_blocked",
        "bizlinks_portal_changed",
    }
)
# Estos dos además activan el cortacircuito (pausa de varias horas).
_CODIGOS_DE_BLOQUEO = (BizlinksBlockedError.code, BizlinksPortalChangedError.code)

BizlinksClientFactory = Callable[..., object]


async def sincronizar_respaldos(
    *,
    uow: UnitOfWork,
    settings: Settings,
    secrets_provider: SecretsProvider,
    client_factory: BizlinksClientFactory,
    access: CompanyMember,
    limit: int = LIMITE_POR_DEFECTO,
    fecha_desde: date | None = None,
    fecha_hasta: date | None = None,
) -> BizlinksSyncOut:
    """Trae el XML y el CDR que falten de los comprobantes emitidos de la empresa."""
    limite = max(1, min(limit, LIMITE_MAXIMO))
    ruc = access.company.ruc
    credenciales = secrets_provider.get_company_credentials(ruc)
    cliente = client_factory(credenciales, settings)
    canal: str = cliente.canal  # type: ignore[attr-defined]
    ahora = datetime.now(tz=UTC)

    cuota_restante: int | None = None
    if canal == "portal":
        pausa = timedelta(hours=settings.bizlinks_portal_cooldown_horas)
        bloqueo = await uow.bizlinks_consultas.ultimo_bloqueo_desde(
            access.company_id, canal, ahora - pausa
        )
        if bloqueo is not None:
            return _sin_trabajo(
                ruc,
                canal,
                ahora,
                detenido_por="bizlinks_cooldown",
                mensaje="Las consultas al portal de Bizlinks están en pausa tras un bloqueo",
                reanuda_at=bloqueo + pausa,
            )
        usados = await uow.bizlinks_consultas.comprobantes_consultados_desde(
            access.company_id, canal, ahora - VENTANA
        )
        cuota_restante = max(0, settings.bizlinks_portal_max_por_dia - usados)
        limite = min(limite, settings.bizlinks_portal_max_por_llamada, cuota_restante)
        if limite == 0:
            return _sin_trabajo(
                ruc,
                canal,
                ahora,
                detenido_por="cuota_diaria_agotada",
                mensaje="Se alcanzó el tope de consultas al portal de Bizlinks en las últimas 24 h",
                cuota_restante=0,
            )

    candidatos = await _candidatos(
        uow,
        company_id=access.company_id,
        canal=canal,
        limite=limite,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        desde_reintento=ahora - VENTANA,
    )

    items: list[BizlinksItemOut] = []
    detenido_por: str | None = None
    reanuda_at: datetime | None = None
    for comprobante, tiene_xml, tiene_cdr in candidatos:
        try:
            item = await _procesar(
                uow=uow,
                settings=settings,
                cliente=cliente,
                canal=canal,
                ruc=ruc,
                comprobante=comprobante,
                tiene_xml=tiene_xml,
                tiene_cdr=tiene_cdr,
            )
        except AppError as exc:
            bloqueo = exc.code in _CODIGOS_DE_BLOQUEO
            await _anotar(
                uow,
                access,
                comprobante,
                canal,
                "bloqueado" if bloqueo else "error",
                exc.details.get("http_status"),
                f"{exc.code}: {redact(exc.message)}",
            )
            items.append(
                BizlinksItemOut(
                    comprobante_id=comprobante.id,
                    numero=comprobante.nombre_archivo or comprobante.numero,
                    resultado="error",
                    mensaje=redact(exc.message),
                    error_code=exc.code,
                )
            )
            if bloqueo:
                reanuda_at = ahora + timedelta(hours=settings.bizlinks_portal_cooldown_horas)
            if exc.code in _CODIGOS_QUE_DETIENEN_EL_LOTE:
                detenido_por = exc.code
                break
            continue
        items.append(item)

    if cuota_restante is not None:
        cuota_restante = max(0, cuota_restante - len(items))
    return BizlinksSyncOut(
        ruc=ruc,
        canal=canal,
        revisados=len(items),
        completos=sum(1 for i in items if i.resultado == "completo"),
        parciales=sum(1 for i in items if i.resultado == "parcial"),
        sin_resultado=sum(1 for i in items if i.resultado == "sin_resultado"),
        fallidos=sum(1 for i in items if i.resultado == "error"),
        xml_guardados=sum(1 for i in items if i.xml_guardado),
        cdr_guardados=sum(1 for i in items if i.cdr_guardado),
        detenido_por=detenido_por,
        cuota_restante=cuota_restante,
        reanuda_at=reanuda_at,
        items=items,
        consultado_at=datetime.now(tz=UTC),
    )


def _sin_trabajo(
    ruc: str,
    canal: str,
    ahora: datetime,
    *,
    detenido_por: str,
    mensaje: str,
    reanuda_at: datetime | None = None,
    cuota_restante: int | None = None,
) -> BizlinksSyncOut:
    return BizlinksSyncOut(
        ruc=ruc,
        canal=canal,
        revisados=0,
        completos=0,
        parciales=0,
        sin_resultado=0,
        fallidos=0,
        xml_guardados=0,
        cdr_guardados=0,
        detenido_por=detenido_por,
        mensaje=mensaje,
        cuota_restante=cuota_restante,
        reanuda_at=reanuda_at,
        items=[],
        consultado_at=ahora,
    )


def _necesita_cdr(comprobante: Comprobante) -> bool:
    """Las boletas no tienen CDR propio: lo recibe el resumen diario."""
    return comprobante.tipo_documento != "03"


async def _candidatos(
    uow: UnitOfWork,
    *,
    company_id: UUID,
    canal: str,
    limite: int,
    fecha_desde: date | None,
    fecha_hasta: date | None,
    desde_reintento: datetime,
) -> list[tuple[Comprobante, bool, bool]]:
    """Emitidos a los que les falta el XML o el CDR, con lo que ya tienen."""
    candidatos: list[tuple[Comprobante, bool, bool]] = []
    revisados = 0
    pagina = 0
    while len(candidatos) < limite and revisados < MAX_REVISADOS:
        pagina_actual = await uow.comprobantes.list_for_company(
            company_id,
            FiltrosListado(
                origen=OrigenComprobante.emitido,
                fecha_desde=fecha_desde,
                fecha_hasta=fecha_hasta,
            ),
            limit=_PAGINA,
            offset=pagina * _PAGINA,
        )
        lote = pagina_actual.items
        if not lote:
            break
        ids = [c.id for c in lote]
        tipos = await uow.archivos.tipos_por_comprobante(ids)
        # Lo que no se encontró (o falló) hace poco no se vuelve a pedir todavía.
        recientes = await uow.bizlinks_consultas.sin_exito_desde(
            company_id, ids, canal, desde_reintento
        )
        for comprobante in lote:
            presentes = tipos.get(comprobante.id, set())
            tiene_xml = TipoArchivo.xml in presentes
            tiene_cdr = TipoArchivo.cdr in presentes or not _necesita_cdr(comprobante)
            if (tiene_xml and tiene_cdr) or comprobante.id in recientes:
                continue
            candidatos.append((comprobante, tiene_xml, tiene_cdr))
            if len(candidatos) >= limite:
                break
        revisados += len(lote)
        pagina += 1
        if len(lote) < _PAGINA:
            break
    return candidatos


async def _anotar(
    uow: UnitOfWork,
    access: CompanyMember,
    comprobante: Comprobante,
    canal: str,
    resultado: str,
    http_status: int | None,
    detalle: str | None,
) -> None:
    await uow.bizlinks_consultas.registrar(
        BizlinksConsultaRegistro(
            company_id=access.company_id,
            comprobante_id=comprobante.id,
            canal=canal,  # type: ignore[arg-type]
            resultado=resultado,  # type: ignore[arg-type]
            http_status=http_status,
            detalle=detalle[:300] if detalle else None,
        )
    )


async def _procesar(
    *,
    uow: UnitOfWork,
    settings: Settings,
    cliente: object,
    canal: str,
    ruc: str,
    comprobante: Comprobante,
    tiene_xml: bool,
    tiene_cdr: bool,
) -> BizlinksItemOut:
    numero = comprobante.nombre_archivo or comprobante.numero
    respaldos: RespaldosBizlinks = await run_blocking(
        cliente.obtener_respaldos,  # type: ignore[attr-defined]
        tipo_documento=comprobante.tipo_documento,
        serie=comprobante.serie,
        correlativo=comprobante.correlativo,
        fecha_emision=comprobante.fecha_emision,
        monto_total=comprobante.monto_total,
        necesita_xml=not tiene_xml,
        necesita_cdr=not tiene_cdr,
    )

    if not respaldos.encontrado:
        await uow.bizlinks_consultas.registrar(
            BizlinksConsultaRegistro(
                company_id=comprobante.company_id,
                comprobante_id=comprobante.id,
                canal=canal,  # type: ignore[arg-type]
                resultado="sin_resultado",
                detalle=("; ".join(respaldos.avisos) or None),
            )
        )
        return BizlinksItemOut(
            comprobante_id=comprobante.id,
            numero=numero,
            resultado="sin_resultado",
            mensaje="; ".join(respaldos.avisos) or "Bizlinks no tiene este comprobante",
        )

    avisos = list(respaldos.avisos)
    xml_guardado = cdr_guardado = False
    items_cargados = 0

    if not tiene_xml and respaldos.xml:
        xml_guardado, items_cargados, aviso = await _guardar_xml(
            uow=uow, settings=settings, ruc=ruc, comprobante=comprobante, xml=respaldos.xml
        )
        if aviso:
            avisos.append(aviso)
    if not tiene_cdr and respaldos.cdr:
        cdr_guardado, aviso = await _guardar_cdr(
            uow=uow, settings=settings, comprobante=comprobante, contenido=respaldos.cdr
        )
        if aviso:
            avisos.append(aviso)

    completo_xml = tiene_xml or xml_guardado
    completo_cdr = tiene_cdr or cdr_guardado
    # Bizlinks sí tenía el archivo pero no se pudo archivar (R2 caído, por ejemplo): eso es
    # un fallo recuperable y NO se marca `sin_resultado`, que lo dejaría 24 h sin reintento.
    fallo_al_archivar = not (xml_guardado or cdr_guardado) and bool(respaldos.xml or respaldos.cdr)
    if completo_xml and completo_cdr:
        resultado = "completo"
    elif xml_guardado or cdr_guardado:
        resultado = "parcial"
    elif fallo_al_archivar:
        resultado = "error"
    else:
        resultado = "sin_resultado"
    await uow.bizlinks_consultas.registrar(
        BizlinksConsultaRegistro(
            company_id=comprobante.company_id,
            comprobante_id=comprobante.id,
            canal=canal,  # type: ignore[arg-type]
            resultado="error" if fallo_al_archivar else "ok",
            detalle=("; ".join(avisos)[:300] or None),
        )
    )
    return BizlinksItemOut(
        comprobante_id=comprobante.id,
        numero=numero,
        resultado=resultado,
        xml_guardado=xml_guardado,
        cdr_guardado=cdr_guardado,
        items_cargados=items_cargados,
        mensaje="; ".join(avisos) or None,
        error_code="storage_error" if fallo_al_archivar else None,
    )


async def _guardar_xml(
    *,
    uow: UnitOfWork,
    settings: Settings,
    ruc: str,
    comprobante: Comprobante,
    xml: bytes,
) -> tuple[bool, int, str | None]:
    """Valida y archiva el XML firmado. Devuelve (guardado, ítems cargados, aviso)."""
    try:
        dato = parse_comprobante_ubl(xml, ruc_esperado=ruc)
    except InvalidDocumentError as exc:
        return False, 0, f"El XML de Bizlinks no es un comprobante válido: {exc.message}"
    if (dato.tipo_documento, dato.serie, dato.correlativo) != (
        comprobante.tipo_documento,
        comprobante.serie,
        comprobante.correlativo,
    ):
        # Nunca se archiva bajo un comprobante un XML que es de otro.
        return False, 0, "El XML descargado no corresponde a este comprobante"

    archivo = await guardar_archivo_sin_fallar(
        uow=uow,
        settings=settings,
        comprobante_id=comprobante.id,
        tipo=TipoArchivo.xml,
        file_bytes=xml,
        filename=f"{comprobante.nombre_archivo or dato.nombre_archivo}.xml",
    )
    if archivo is None:
        return False, 0, "No se pudo archivar el XML en el almacenamiento"

    items_cargados = 0
    if dato.items and not await uow.items.list_for_comprobante(comprobante.id):
        creados = await uow.items.create_many(items_a_registros(comprobante.id, dato.items))
        items_cargados = len(creados)
    return True, items_cargados, None


def _leer_cdr(contenido: bytes) -> tuple[CdrInfo, bytes]:
    """CDR interpretado y el ZIP a archivar (Bizlinks puede entregarlo como XML suelto)."""
    if contenido.startswith(b"PK"):
        cdr = parse_cdr_zip(contenido)
        return cdr, contenido
    cdr = parse_cdr_xml(contenido)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("cdr.xml", contenido)
    return cdr, buffer.getvalue()


async def _guardar_cdr(
    *,
    uow: UnitOfWork,
    settings: Settings,
    comprobante: Comprobante,
    contenido: bytes,
) -> tuple[bool, str | None]:
    try:
        cdr, zip_bytes = _leer_cdr(contenido)
    except AppError as exc:
        return False, f"El CDR de Bizlinks no se pudo interpretar: {exc.message}"

    archivo = await guardar_archivo_sin_fallar(
        uow=uow,
        settings=settings,
        comprobante_id=comprobante.id,
        tipo=TipoArchivo.cdr,
        file_bytes=zip_bytes,
        filename=f"{comprobante.nombre_archivo or comprobante.numero}-CDR.zip",
    )
    if archivo is None:
        return False, "No se pudo archivar el CDR en el almacenamiento"

    estado = siguiente_estado(comprobante.estado, map_cdr_a_estado(cdr))
    await uow.comprobantes.update_sunat_state(
        comprobante.id,
        estado=estado,
        cdr_codigo=cdr.codigo,
        cdr_descripcion=cdr.descripcion,
        cdr_notas=cdr.notas,
    )
    return True, None
