"""Servicio de la prueba de conexión/credenciales contra SUNAT (`/sunat/status`)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from app.core.concurrency import run_blocking
from app.core.config import Settings
from app.core.enums import RolEmpresa, SunatAmbiente, TipoOperacionSunat
from app.core.errors import CredentialsNotConfiguredError
from app.core.redaction import redact
from app.core.security import AuthenticatedUser
from app.db.models import CompanyMember, SunatEnvioRegistro
from app.db.repositories.base import UnitOfWork
from app.secrets.base import CompanyCredentials, SecretsProvider
from app.secrets.certificate import inspect_certificate
from app.services.results import (
    CertificateOut,
    CredencialesOut,
    DetalleSunatOut,
    SunatStatusOut,
)
from app.sunat.consult import SunatConsultClient
from app.sunat.models import ResultadoDisponibilidad

SunatClientFactory = Callable[[CompanyCredentials, SunatAmbiente], SunatConsultClient]


async def check_company_sunat_status(
    *,
    uow: UnitOfWork,
    settings: Settings,
    secrets_provider: SecretsProvider,
    client_factory: SunatClientFactory,
    user: AuthenticatedUser,
    access: CompanyMember,
) -> SunatStatusOut:
    """Prueba de conexión y credenciales de una empresa.

    Nunca lanza por credenciales inválidas: el frontend necesita distinguir
    "no autenticó" de "SUNAT no responde" para poder mostrarlo.
    """
    ruc = access.company.ruc
    env = settings.effective_sunat_env(access.company.sunat_ambiente)
    consultado_at = datetime.now(tz=UTC)

    try:
        credentials = secrets_provider.get_company_credentials(ruc)
    except CredentialsNotConfiguredError as exc:
        return SunatStatusOut(
            ruc=ruc,
            razon_social=access.company.razon_social,
            sunat_env=env,
            estado="sin_credenciales",
            autenticado=False,
            detalle=DetalleSunatOut(mensaje=exc.message),
            credenciales=CredencialesOut(
                sol_user_configurado=False,
                api_rest_configurada=False,
                certificado=CertificateOut(configurado=False, abre_con_clave=False),
            ),
            consultado_at=consultado_at,
        )

    certificado = inspect_certificate(credentials)
    credenciales_out = CredencialesOut(
        sol_user_configurado=True,
        api_rest_configurada=credentials.has_api_credentials,
        certificado=CertificateOut(
            configurado=certificado.configurado,
            abre_con_clave=certificado.abre_con_clave,
            origen=certificado.origen,
            sujeto=certificado.sujeto,
            emisor=certificado.emisor,
            numero_serie=certificado.numero_serie,
            valido_desde=certificado.valido_desde,
            valido_hasta=certificado.valido_hasta,
            dias_para_vencer=certificado.dias_para_vencer,
            vigente=certificado.vigente,
            error=certificado.error,
        ),
    )

    client = client_factory(credentials, env)
    # La llamada a SUNAT es síncrona (zeep): se ejecuta en un hilo para no
    # congelar el event loop durante el timeout.
    resultado = await run_blocking(client.check_availability)
    estado = _estado_desde_resultado(resultado)

    intento = await _registrar_intento(
        uow=uow,
        company_id=access.company.id,
        env=env,
        resultado=resultado,
        usuario=user,
    )

    return SunatStatusOut(
        ruc=ruc,
        razon_social=access.company.razon_social,
        sunat_env=env,
        estado=estado,
        autenticado=resultado.autenticado,
        metodo=resultado.metodo,
        endpoint=resultado.endpoint,
        detalle=DetalleSunatOut(
            codigo=resultado.codigo,
            mensaje=redact(resultado.mensaje) if resultado.mensaje else None,
        ),
        credenciales=credenciales_out,
        latencia_ms=resultado.duracion_ms,
        intento_id=intento.id if intento else None,
        consultado_at=consultado_at,
    )


def _estado_desde_resultado(resultado: ResultadoDisponibilidad) -> str:
    if resultado.autenticado:
        return "autenticado"
    if resultado.error_tipo == "sunat_auth_error":
        return "credenciales_invalidas"
    return "servicio_no_disponible"


async def _registrar_intento(
    *,
    uow: UnitOfWork,
    company_id,
    env: SunatAmbiente,
    resultado: ResultadoDisponibilidad,
    usuario: AuthenticatedUser,
):
    """Registra el intento en `sunat_envios`. Un fallo aquí no debe romper la respuesta."""
    registro = SunatEnvioRegistro(
        company_id=company_id,
        comprobante_id=None,
        tipo_operacion=TipoOperacionSunat.prueba_conexion,
        sunat_ambiente=env,
        endpoint=resultado.endpoint,
        metodo=resultado.metodo,
        request_xml=resultado.request_xml,
        response_xml=resultado.response_xml,
        http_status=resultado.http_status,
        codigo_respuesta=resultado.codigo,
        mensaje_respuesta=redact(resultado.mensaje) if resultado.mensaje else None,
        exito=resultado.autenticado,
        error_tipo=resultado.error_tipo,
        duracion_ms=resultado.duracion_ms,
    )
    return await registrar_intento_seguro(uow, registro, usuario)


async def registrar_intento_seguro(uow: UnitOfWork, registro: SunatEnvioRegistro, usuario):
    """Inserta el intento sin propagar errores de bitácora (se registran en el log)."""
    from app.core.logging import get_logger

    logger = get_logger(__name__)
    try:
        return await uow.sunat_envios.record(registro)
    except Exception:
        logger.exception(
            "No se pudo registrar el intento de SUNAT (empresa %s, operación %s, usuario %s)",
            registro.company_id,
            registro.tipo_operacion.value,
            usuario.id,
        )
        return None


__all__ = [
    "ROLES_CONSULTA_SUNAT",
    "SunatClientFactory",
    "check_company_sunat_status",
    "registrar_intento_seguro",
]

ROLES_CONSULTA_SUNAT = frozenset({RolEmpresa.admin, RolEmpresa.emisor})
