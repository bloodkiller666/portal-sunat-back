"""Servicio `refresh-cdr`: consulta SUNAT, actualiza el estado y registra el intento."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from app.core.config import Settings
from app.core.enums import ROLES_OPERATIVOS, EstadoComprobante, SunatAmbiente, TipoOperacionSunat
from app.core.errors import NotFoundError, SunatError
from app.core.logging import get_logger
from app.core.redaction import redact
from app.core.security import AuthenticatedUser
from app.db.models import Comprobante, SunatEnvio, SunatEnvioRegistro
from app.db.repositories.base import UnitOfWork
from app.secrets.base import SecretsProvider
from app.services.companies import require_membership
from app.services.results import CdrOut, IntentoOut, RefreshCdrOut
from app.services.sunat_status import SunatClientFactory, registrar_intento_seguro
from app.sunat.models import ResultadoConsulta, TramasSOAP
from app.sunat.states import map_cdr_a_estado, map_codigo_a_estado

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

    env = settings.effective_sunat_env(comprobante.sunat_ambiente)
    credentials = secrets_provider.get_company_credentials(comprobante.ruc_emisor)

    try:
        client = client_factory(credentials, env)
        resultado = client.get_status_cdr(
            tipo_comprobante=comprobante.tipo_documento,
            serie=comprobante.serie,
            numero=comprobante.numero,
            monto_total=comprobante.monto_total,
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


def _siguiente_estado(actual: EstadoComprobante, propuesto: EstadoComprobante) -> EstadoComprobante:
    """Una consulta de estado nunca revierte una anulación."""
    if actual is EstadoComprobante.anulado and propuesto is not EstadoComprobante.anulado:
        logger.info("Se conserva el estado Anulado del comprobante consultado")
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
