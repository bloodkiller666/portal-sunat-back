"""Aplicación FastAPI del Portal de Facturación Electrónica."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.routers import archivos, companies, comprobantes, health
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging, get_logger, get_request_id
from app.core.middleware import register_cors, register_request_context
from app.core.redaction import redact
from app.core.security import JwtVerifier
from app.db.pool import Database
from app.db.uow import build_uow_factory
from app.secrets.factory import build_secrets_provider
from app.sunat.consult import SunatConsultClient

logger = get_logger(__name__)

OPENAPI_TAGS = [
    {"name": "salud", "description": "Estado del servicio (público, sin token)."},
    {"name": "empresas", "description": "Empresas del usuario y prueba de credenciales SUNAT."},
    {"name": "comprobantes", "description": "Comprobantes y sincronización del estado con SUNAT."},
    {"name": "archivos", "description": "XML, PDF y CDR del comprobante en el bucket privado."},
]

API_DESCRIPTION = """
Backend del Portal de Facturación Electrónica. Integración directa con los
servicios web oficiales de SUNAT (SOAP `billService` / `billConsultService`),
sin intermediarios.

* Todos los endpoints salvo `/health` exigen un JWT de Supabase Auth y verifican
  que el usuario pertenezca a la empresa solicitada.
* Las credenciales por empresa (usuario y clave SOL, certificado .pfx) se
  resuelven con un `SecretsProvider`; nunca se guardan en la base ni en el código.
* Esta fase es de **consulta**: la emisión, la firma del XML, el PDF, SAP y el
  envío por correo están fuera de alcance.
""".strip()


def crear_cliente_sunat(settings: Settings) -> Callable[..., SunatConsultClient]:
    """Fábrica del cliente SOAP. Se sustituye en pruebas para no tocar la red."""

    def factory(credentials, env=None) -> SunatConsultClient:
        return SunatConsultClient(credentials, settings, env=env)

    return factory


def _detalles_seguros(details: dict[str, Any]) -> dict[str, Any]:
    """Expone solo detalles planos y sin tramas SOAP ni secretos."""
    seguros: dict[str, Any] = {}
    for clave, valor in (details or {}).items():
        if clave in {"tramas", "request_xml", "response_xml"}:
            continue
        if valor is None or isinstance(valor, (str, int, float, bool)):
            seguros[clave] = redact(valor) if isinstance(valor, str) else valor
    return seguros


def _registrar_manejadores(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_request: Request, exc: AppError) -> JSONResponse:
        payload = {
            "error": {
                "code": exc.code,
                "message": redact(exc.message),
                "details": _detalles_seguros(exc.details),
                "request_id": get_request_id(),
            }
        }
        if exc.status_code >= 500:
            logger.error("Error %s: %s", exc.code, exc.message)
        else:
            logger.info("Error %s: %s", exc.code, exc.message)
        return JSONResponse(status_code=exc.status_code, content=payload, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validacion(_request: Request, exc: RequestValidationError) -> JSONResponse:
        errores = [
            {
                "campo": ".".join(str(parte) for parte in error.get("loc", [])),
                "tipo": error.get("type"),
                "mensaje": error.get("msg"),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "La petición no cumple el formato esperado",
                    "details": {"errores": errores},
                    "request_id": get_request_id(),
                }
            },
        )

    @app.exception_handler(Exception)
    async def _inesperado(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Error no controlado")
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "Error interno del servidor",
                    "details": {},
                    "request_id": get_request_id(),
                }
            },
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Construye la aplicación. `settings` se inyecta en las pruebas."""
    settings = settings or get_settings()
    configure_logging(settings)

    database = Database(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        for advertencia in settings.startup_warnings():
            logger.warning(advertencia)
        if database.configured:
            try:
                await database.connect()
            except Exception:
                logger.exception(
                    "No se pudo conectar a Supabase; los endpoints con datos responderán 503"
                )
        yield
        await database.close()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=API_DESCRIPTION,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
    )

    app.state.settings = settings
    app.state.database = database
    app.state.jwt_verifier = JwtVerifier(settings)
    app.state.secrets_provider = build_secrets_provider(settings)
    app.state.uow_factory = build_uow_factory(database)
    app.state.sunat_client_factory = crear_cliente_sunat(settings)

    register_cors(app, settings)
    register_request_context(app)
    _registrar_manejadores(app)

    app.include_router(health.router)
    app.include_router(companies.router, prefix=settings.api_v1_prefix)
    app.include_router(comprobantes.router, prefix=settings.api_v1_prefix)
    app.include_router(archivos.router, prefix=settings.api_v1_prefix)

    logger.info(
        "API lista (%s, SUNAT_ENV=%s, docs=%s)",
        settings.environment.value,
        settings.sunat_env.value,
        "habilitados" if settings.docs_enabled else "deshabilitados",
    )
    return app


app = create_app()
