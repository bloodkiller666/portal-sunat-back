"""Errores de dominio y su traducción a respuestas HTTP.

Regla: los mensajes de error nunca incluyen claves, tokens ni el contenido del
certificado. Los detalles técnicos se registran en los logs ya redactados.
"""

from __future__ import annotations

from typing import Any, ClassVar


class AppError(Exception):
    """Error base de la aplicación."""

    status_code: ClassVar[int] = 500
    code: ClassVar[str] = "internal_error"
    default_message: ClassVar[str] = "Error interno del servidor"

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.details = details or {}
        self.headers = headers or {}
        super().__init__(self.message)


class ConfigError(AppError):
    status_code = 500
    code = "config_error"
    default_message = "La configuración del servicio está incompleta"


class DatabaseNotConfiguredError(AppError):
    status_code = 503
    code = "database_not_configured"
    default_message = "La base de datos no está configurada en este entorno"


class AuthNotConfiguredError(AppError):
    """Falta configurar la verificación del token: es un fallo del servidor, no del cliente."""

    status_code = 503
    code = "auth_not_configured"
    default_message = (
        "La verificación del token no está configurada en el servidor: defina "
        "SUPABASE_JWT_SECRET (HS256) o el JWKS de Supabase (ES256/RS256)"
    )


class AuthenticationError(AppError):
    status_code = 401
    code = "unauthenticated"
    default_message = "Token de acceso ausente o inválido"

    def __init__(self, message: str | None = None, **kwargs: Any) -> None:
        headers = {"WWW-Authenticate": "Bearer"}
        headers.update(kwargs.pop("headers", {}) or {})
        super().__init__(message, headers=headers, **kwargs)


class AuthorizationError(AppError):
    status_code = 403
    code = "forbidden"
    default_message = "No tiene permisos para esta operación"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"
    default_message = "El recurso solicitado no existe"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"
    default_message = "La operación entra en conflicto con el estado actual"


class CredentialsNotConfiguredError(AppError):
    status_code = 409
    code = "credentials_not_configured"
    default_message = "La empresa no tiene credenciales SUNAT configuradas"


class ProdEmissionBlockedError(AppError):
    """Regla 4: en producción la emisión está bloqueada sin confirmación explícita."""

    status_code = 403
    code = "prod_emission_blocked"
    default_message = (
        "La emisión en el ambiente de producción está bloqueada. "
        "Defina CONFIRM_PROD_EMISSION=true para habilitarla."
    )


class SunatError(AppError):
    status_code = 502
    code = "sunat_error"
    default_message = "Error al comunicarse con los servicios de SUNAT"


class SunatAuthError(SunatError):
    status_code = 502
    code = "sunat_auth_error"
    default_message = "SUNAT rechazó las credenciales SOL de la empresa"


class SunatFaultError(SunatError):
    status_code = 502
    code = "sunat_fault"
    default_message = "SUNAT devolvió una respuesta de error"


class SunatUnavailableError(SunatError):
    status_code = 503
    code = "sunat_unavailable"
    default_message = "El servicio de SUNAT no está disponible en este momento"


class SunatConsultNotAvailableError(SunatUnavailableError):
    code = "sunat_consult_unavailable"
    default_message = (
        "El servicio de consulta de CDR (billConsultService) solo está publicado "
        "en el ambiente de producción de SUNAT"
    )


class CdrParseError(SunatError):
    code = "cdr_parse_error"
    default_message = "No se pudo interpretar el CDR devuelto por SUNAT"


class CdrNotAvailableError(AppError):
    status_code = 404
    code = "cdr_not_available"
    default_message = "SUNAT todavía no tiene un CDR disponible para este comprobante"
