"""Pruebas del almacenamiento en Cloudflare R2 (bucket privado, URLs firmadas).

No se toca la red: el cliente S3 se reemplaza por un doble en memoria.
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.core.enums import EstadoArchivo, RolEmpresa, TipoArchivo
from app.core.errors import StorageNotConfiguredError
from app.services.store import sube_archivo, url_descarga
from app.storage import construir_clave, get_storage_provider
from app.storage.providers.r2 import CloudflareR2StorageProvider
from tests.conftest import RUC_PRINCIPAL
from tests.fakes import (
    FakeStorageProvider,
    FakeUnitOfWork,
    construir_comprobante,
    construir_empresa,
    construir_membresia,
)


def _settings_r2(**overrides: object) -> Settings:
    base = {
        "r2_account_id": "cuenta",
        "r2_access_key_id": "AKID",
        "r2_secret_access_key": "SECRET",
        "r2_bucket_name": "comprobantes-privado",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


class _FakeS3Client:
    """Doble del cliente S3: registra las llamadas y firma URLs ficticias."""

    def __init__(self, llamadas: list[tuple[str, dict]]) -> None:
        self._llamadas = llamadas

    async def put_object(self, **kwargs: object) -> None:
        self._llamadas.append(("put_object", kwargs))

    async def generate_presigned_url(
        self, operacion: str, *, Params: dict, ExpiresIn: int  # noqa: N803 - firma de boto3
    ) -> str:
        self._llamadas.append(
            (
                "generate_presigned_url",
                {"operacion": operacion, "Params": Params, "ExpiresIn": ExpiresIn},
            )
        )
        return f"https://signed.example/{Params['Key']}?expira={ExpiresIn}"

    async def __aenter__(self) -> _FakeS3Client:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False


def _provider_con_cliente(
    monkeypatch: pytest.MonkeyPatch, **overrides: object
) -> tuple[CloudflareR2StorageProvider, list[tuple[str, dict]]]:
    proveedor = CloudflareR2StorageProvider(settings=_settings_r2(**overrides))
    llamadas: list[tuple[str, dict]] = []
    monkeypatch.setattr(proveedor, "_client", lambda: _FakeS3Client(llamadas))
    return proveedor, llamadas


def test_clave_se_sanea_y_no_duplica_barras() -> None:
    assert construir_clave("/comprobantes//2026/10/", "F001-1.xml") == (
        "comprobantes/2026/10/F001-1.xml"
    )
    # Nada de path traversal ni separadores inyectados en el nombre.
    traversal = construir_clave("comprobantes", "../../etc/passwd")
    assert ".." not in traversal
    assert traversal.startswith("comprobantes/")
    assert construir_clave("", "a b.xml") == "a_b.xml"


def test_endpoint_se_deriva_de_la_cuenta() -> None:
    assert _settings_r2().r2_endpoint == "https://cuenta.r2.cloudflarestorage.com"
    assert _settings_r2(r2_endpoint_url="https://minio.local/").r2_endpoint == "https://minio.local"


def test_proveedor_sin_configuracion_falla_con_503() -> None:
    with pytest.raises(StorageNotConfiguredError) as error:
        get_storage_provider(Settings(_env_file=None))
    assert error.value.status_code == 503
    assert error.value.code == "storage_not_configured"


def test_proveedor_configurado_se_construye() -> None:
    assert isinstance(get_storage_provider(_settings_r2()), CloudflareR2StorageProvider)


async def test_upload_sube_al_bucket_privado_y_calcula_el_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proveedor, llamadas = _provider_con_cliente(monkeypatch)

    resultado = await proveedor.upload_file(
        b"<xml/>", "F001-1.xml", "application/xml", "comprobantes/20123456789/2026/10"
    )

    assert resultado.file_key == "comprobantes/20123456789/2026/10/F001-1.xml"
    assert resultado.content_type == "application/xml"
    assert resultado.size_bytes == len(b"<xml/>")
    assert resultado.hash_sha256 == hashlib.sha256(b"<xml/>").hexdigest()

    operacion, kwargs = llamadas[0]
    assert operacion == "put_object"
    assert kwargs["Bucket"] == "comprobantes-privado"
    assert kwargs["Key"] == resultado.file_key


async def test_get_download_url_firma_una_url_temporal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proveedor, llamadas = _provider_con_cliente(monkeypatch)

    url = await proveedor.get_download_url("comprobantes/a.xml", expires_in=120)

    assert url == "https://signed.example/comprobantes/a.xml?expira=120"
    operacion, kwargs = llamadas[0]
    assert operacion == "generate_presigned_url"
    assert kwargs["operacion"] == "get_object"
    assert kwargs["Params"] == {"Bucket": "comprobantes-privado", "Key": "comprobantes/a.xml"}
    assert kwargs["ExpiresIn"] == 120


async def test_usa_la_expiracion_configurada_por_defecto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proveedor, llamadas = _provider_con_cliente(monkeypatch, r2_signed_url_expires_seconds=3600)
    await proveedor.get_download_url("k")
    assert llamadas[0][1]["ExpiresIn"] == 3600


@pytest.fixture
def uow_con_comprobante() -> tuple[FakeUnitOfWork, object]:
    empresa = construir_empresa(ruc=RUC_PRINCIPAL)
    comprobante = construir_comprobante(empresa.id, ruc=RUC_PRINCIPAL, correlativo=7)
    unidad = FakeUnitOfWork()
    unidad.companies.empresas = [empresa]
    unidad.companies.membresias = [
        construir_membresia(uuid4(), empresa.id, RolEmpresa.emisor),
    ]
    unidad.comprobantes.comprobantes[comprobante.id] = comprobante
    unidad.comprobantes.company_ids_visibles = {empresa.id}
    return unidad, comprobante


async def test_sube_archivo_registra_hash_mime_y_clave(
    uow_con_comprobante: tuple[FakeUnitOfWork, object],
) -> None:
    uow, comprobante = uow_con_comprobante
    proveedor = FakeStorageProvider()

    archivo = await sube_archivo(
        uow=uow,
        settings=_settings_r2(),
        comprobante_id=comprobante.id,
        tipo=TipoArchivo.xml,
        file_bytes=b"<xml/>",
        storage_provider=proveedor,
    )

    assert archivo.mime_type == "application/xml"
    assert archivo.size_bytes == 6
    assert archivo.hash_sha256 == hashlib.sha256(b"<xml/>").hexdigest()
    assert archivo.storage_provider == "r2"
    assert archivo.storage_bucket == "comprobantes-privado"
    assert archivo.storage_key.startswith(f"comprobantes/{RUC_PRINCIPAL}/")
    assert archivo.storage_key.endswith(".xml")
    assert archivo.estado is EstadoArchivo.subido
    assert uow.archivos.archivos == [archivo]
    # El MIME es el real, no una URL; el hash es el sha256 del contenido.
    assert not archivo.mime_type.startswith("http")
    assert proveedor.upload["nombre"].endswith(".xml")


async def test_url_descarga_firma_con_el_proveedor(
    uow_con_comprobante: tuple[FakeUnitOfWork, object],
) -> None:
    uow, comprobante = uow_con_comprobante
    archivo = await sube_archivo(
        uow=uow,
        settings=_settings_r2(),
        comprobante_id=comprobante.id,
        tipo=TipoArchivo.xml,
        file_bytes=b"<xml/>",
        storage_provider=FakeStorageProvider(),
    )

    url = await url_descarga(
        settings=_settings_r2(), archivo=archivo, storage_provider=FakeStorageProvider()
    )

    assert url == f"https://firmada/{archivo.storage_key}"
