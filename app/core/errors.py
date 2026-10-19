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


class StorageError(AppError):
    status_code = 502
    code = "storage_error"
    default_message = "No se pudo almacenar el archivo"


class StorageNotConfiguredError(AppError):
    status_code = 503
    code = "storage_not_configured"
    default_message = (
        "El almacenamiento de archivos no está configurado en este entorno "
        "(R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY y R2_BUCKET_NAME)"
    )


class FileTooLargeError(AppError):
    status_code = 413
    code = "file_too_large"
    default_message = "El archivo supera el tamaño máximo permitido"


class InvalidDocumentError(AppError):
    """El XML recibido no es un comprobante UBL válido o no corresponde a la empresa."""

    status_code = 422
    code = "invalid_document"
    default_message = "El archivo no es un comprobante electrónico válido"


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


class BizlinksError(AppError):
    """Falla al comunicarse con Bizlinks (OSE) o una respuesta inesperada."""

    status_code = 502
    code = "bizlinks_error"
    default_message = "Error al comunicarse con Bizlinks"


class BizlinksNotConfiguredError(BizlinksError):
    status_code = 409
    code = "bizlinks_not_configured"
    default_message = (
        "Bizlinks no está configurado: defina BIZLINKS_SERVICE_URL y las credenciales "
        "SUNAT_CRED_<RUC>_BIZLINKS_USER / _BIZLINKS_PASSWORD (o use BIZLINKS_CANAL=portal)"
    )


class BizlinksAuthError(BizlinksError):
    status_code = 502
    code = "bizlinks_auth_error"
    default_message = "Bizlinks rechazó las credenciales de la empresa"


class BizlinksUnavailableError(BizlinksError):
    status_code = 503
    code = "bizlinks_unavailable"
    default_message = "El servicio de Bizlinks no está disponible en este momento"


class BizlinksBlockedError(BizlinksError):
    """El portal anónimo bloqueó la consulta (403, 429 o CAPTCHA). No se intenta saltar."""

    status_code = 503
    code = "bizlinks_blocked"
    default_message = "Bizlinks bloqueó las consultas: se pausan por un tiempo"


class BizlinksPortalChangedError(BizlinksError):
    """La página del portal ya no tiene la forma esperada: no se adivina ni se sigue."""

    status_code = 502
    code = "bizlinks_portal_changed"
    default_message = "El portal de Bizlinks cambió y la consulta automática ya no es válida"


class CdrNotAvailableError(AppError):
    status_code = 404
    code = "cdr_not_available"
    default_message = "SUNAT todavía no tiene un CDR disponible para este comprobante"
