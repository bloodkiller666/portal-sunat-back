"""Almacenamiento en Cloudflare R2 (API S3 compatible) sobre bucket privado.

R2 expone un endpoint S3 por cuenta:
`https://<account_id>.r2.cloudflarestorage.com`. La autenticación es por
`Access Key ID` + `Secret Access Key` (token de API de R2).

El bucket es **privado**: `upload_file` sube el objeto y `get_download_url`
firma una URL `GET` temporal. Nunca se expone una URL pública ni se guardan
credenciales en la base de datos.
"""

from __future__ import annotations

import hashlib

from app.core.config import Settings
from app.core.errors import StorageError, StorageNotConfiguredError
from app.core.logging import get_logger
from app.core.redaction import register_secret_values
from app.storage.base import (
    FileUploadResult,
    StorageProvider,
    construir_clave,
    normalizar_clave,
)

logger = get_logger(__name__)

_REGION_R2 = "auto"

_SESION: object | None = None


def _sesion_compartida():
    """Una sola sesión de aioboto3 para todo el proceso.

    Crear una sesión carga los modelos de servicio de botocore (~0,8 s); como el proveedor se arma
    en cada petición, una sesión por proveedor retrasaba cada firma o descarga casi un segundo.
    """
    global _SESION
    if _SESION is None:
        import aioboto3

        _SESION = aioboto3.Session()
    return _SESION


class CloudflareR2StorageProvider(StorageProvider):
    """Guarda los archivos en Cloudflare R2 mediante la API S3 compatible."""

    def __init__(self, *, settings: Settings) -> None:
        self._settings = settings
        if not settings.r2_configured:
            raise StorageNotConfiguredError(
                details={
                    "endpoint_configurado": settings.r2_endpoint is not None,
                    "bucket_configurado": settings.r2_bucket_name is not None,
                }
            )

        self._bucket = settings.r2_bucket_name or ""
        self._endpoint = settings.r2_endpoint
        self._access_key_id = settings.r2_access_key_id
        self._secret_access_key = settings.r2_secret_access_key
        self._expires_in = settings.r2_signed_url_expires_seconds

        # Regla 2: que las credenciales de R2 nunca aparezcan en los logs.
        register_secret_values(self._access_key_id, self._secret_access_key)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    async def upload_file(
        self,
        file_bytes: bytes,
        filename: str,
        content_type: str,
        folder_path: str,
    ) -> FileUploadResult:
        """Sube el objeto al bucket privado y devuelve su clave y hash."""
        file_key = construir_clave(folder_path, filename)
        try:
            async with self._client() as client:
                await client.put_object(
                    Bucket=self._bucket,
                    Key=file_key,
                    Body=file_bytes,
                    ContentType=content_type,
                )
        except Exception as exc:  # botocore expone varias subclases de error
            logger.warning("Fallo al subir un archivo a R2 (%s)", type(exc).__name__)
            raise StorageError(
                "No se pudo subir el archivo al almacenamiento",
                details={"detalle": type(exc).__name__},
            ) from exc

        return FileUploadResult(
            file_key=file_key,
            size_bytes=len(file_bytes),
            content_type=content_type,
            hash_sha256=hashlib.sha256(file_bytes).hexdigest(),
        )

    async def get_download_url(
        self, file_key: str, *, expires_in: int | None = None, inline_mime: str | None = None
    ) -> str:
        """Firma una URL `GET` temporal para el objeto (bucket privado)."""
        expiracion = expires_in or self._expires_in
        try:
            clave = normalizar_clave(file_key)
        except ValueError as exc:
            raise StorageError("La clave del archivo no es válida") from exc
        try:
            async with self._client() as client:
                parametros = {"Bucket": self._bucket, "Key": clave}
                if inline_mime:
                    parametros["ResponseContentType"] = inline_mime
                    parametros["ResponseContentDisposition"] = "inline"
                url = await client.generate_presigned_url(
                    "get_object", Params=parametros, ExpiresIn=expiracion
                )
        except Exception as exc:
            logger.warning("Fallo al firmar una URL de R2 (%s)", type(exc).__name__)
            raise StorageError(
                "No se pudo generar la URL de descarga del archivo",
                details={"detalle": type(exc).__name__},
            ) from exc
        return url

    async def get_object_bytes(self, file_key: str) -> bytes:
        """Descarga el objeto completo del bucket privado."""
        try:
            clave = normalizar_clave(file_key)
        except ValueError as exc:
            raise StorageError("La clave del archivo no es válida") from exc
        try:
            async with self._client() as client:
                respuesta = await client.get_object(Bucket=self._bucket, Key=clave)
                async with respuesta["Body"] as cuerpo:
                    return await cuerpo.read()
        except Exception as exc:
            logger.warning("Fallo al leer un archivo de R2 (%s)", type(exc).__name__)
            raise StorageError(
                "No se pudo leer el archivo del almacenamiento",
                details={"detalle": type(exc).__name__},
            ) from exc

    # ------------------------------------------------------------------ #
    # Internos
    # ------------------------------------------------------------------ #
    def _client(self):
        """Cliente S3 de aioboto3 (context manager asíncrono)."""
        return _sesion_compartida().client(
            "s3",
            endpoint_url=self._endpoint,
            aws_access_key_id=(
                self._access_key_id.get_secret_value() if self._access_key_id else None
            ),
            aws_secret_access_key=(
                self._secret_access_key.get_secret_value() if self._secret_access_key else None
            ),
            region_name=_REGION_R2,
            config=_client_config(),
        )


def _client_config():
    """Reintentos acotados en el cliente S3 (los timeouts los controla botocore)."""
    from botocore.config import Config

    return Config(retries={"max_attempts": 3, "mode": "standard"})
