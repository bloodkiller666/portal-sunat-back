"""Pruebas de la reorganización de rutas en R2 (sin red ni base real)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from scripts.reorganizar_r2 import Movimiento, aplicar, clave_reorganizada, planificar

from app.core.config import Settings
from tests.conftest import RUC_PRINCIPAL

RUC = RUC_PRINCIPAL


@pytest.mark.parametrize(
    "clave",
    [
        f"comprobantes/{RUC}/2026/09/20514831620-01-FF01-00015979.pdf",
        f"comprobantes/{RUC}/2026/10/20514831620-01-FF01-00015980.xml",
        f"comprobantes/{RUC}/2026/10/20514831620-01-FF01-00015980-CDR.zip",
    ],
)
def test_el_archivo_pasa_a_la_carpeta_del_documento(clave: str) -> None:
    destino = clave_reorganizada(clave)

    assert destino is not None
    carpeta, nombre = clave.rsplit("/", 1)
    serie, numero = nombre.split(".")[0].removesuffix("-CDR").split("-")[2:4]
    assert destino == f"{carpeta}/{serie}-{numero}/{nombre}"


def test_el_xml_y_el_cdr_del_mismo_documento_comparten_carpeta() -> None:
    xml = clave_reorganizada(f"comprobantes/{RUC}/2026/10/20514831620-01-FF01-00015980.xml")
    cdr = clave_reorganizada(f"comprobantes/{RUC}/2026/10/20514831620-01-FF01-00015980-CDR.zip")

    assert xml is not None and cdr is not None
    assert xml.rsplit("/", 1)[0] == cdr.rsplit("/", 1)[0]


@pytest.mark.parametrize(
    "clave",
    [
        # Ya tiene su carpeta: no se toca (el script es idempotente).
        f"comprobantes/{RUC}/2026/10/FF01-00015980/20514831620-01-FF01-00015980.xml",
        # Evidencia de SIRE: otro esquema a propósito.
        "sire/20514831620/202609/RV-202609.zip",
        # Nombre que no sigue la nomenclatura de SUNAT: mejor no adivinar.
        f"comprobantes/{RUC}/2026/10/archivo.zip",
    ],
)
def test_lo_que_no_aplica_se_deja_igual(clave: str) -> None:
    assert clave_reorganizada(clave) is None


class _Paginador:
    def __init__(self, claves: list[str]) -> None:
        self._claves = claves

    async def paginate(self, **kwargs: object):
        prefijo = str(kwargs.get("Prefix") or "")
        yield {"Contents": [{"Key": c} for c in self._claves if c.startswith(prefijo)]}


class _R2:
    def __init__(self, claves: list[str]) -> None:
        self._claves = claves
        self.copiados: list[tuple[str, str]] = []
        self.borrados: list[str] = []

    def get_paginator(self, _operacion: str) -> _Paginador:
        return _Paginador(self._claves)

    async def copy_object(self, **kwargs: object) -> None:
        self.copiados.append((kwargs["CopySource"]["Key"], kwargs["Key"]))  # type: ignore[index]

    async def delete_object(self, **kwargs: object) -> None:
        self.borrados.append(kwargs["Key"])  # type: ignore[arg-type]

    async def __aenter__(self) -> _R2:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Proveedor:
    def __init__(self, r2: _R2) -> None:
        self._r2 = r2

    def _client(self) -> _R2:
        return self._r2


class _Conexion:
    def __init__(self, con_fila: set[str]) -> None:
        self._con_fila = con_fila
        self.actualizadas: list[tuple[str, str]] = []
        self.cerrada = False

    async def fetchval(self, _sql: str, destino: str, origen: str):
        self.actualizadas.append((origen, destino))
        return uuid4() if origen in self._con_fila else None

    async def close(self) -> None:
        self.cerrada = True


class _Asyncpg:
    def __init__(self, conexion: _Conexion) -> None:
        self._conexion = conexion

    async def connect(self, *_args: object, **_kwargs: object) -> _Conexion:
        return self._conexion


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        r2_account_id="cuenta",
        r2_access_key_id="AKID",
        r2_secret_access_key="SECRET",
        r2_bucket_name="bucket-privado",
    )


async def test_planificar_separa_lo_que_se_mueve_de_lo_que_se_deja() -> None:
    claves = [
        f"comprobantes/{RUC}/2026/10/20514831620-01-FF01-00015980.xml",
        f"comprobantes/{RUC}/2026/10/FF01-00015981/20514831620-01-FF01-00015981.xml",
        "sire/20514831620/202609/RV-202609.zip",
    ]

    movimientos, ignoradas, existentes = await planificar(
        _Proveedor(_R2(claves)), "bucket-privado", "comprobantes/"  # type: ignore[arg-type]
    )

    assert [m.origen for m in movimientos] == [claves[0]]
    assert ignoradas == [claves[1]]
    assert existentes == set(claves[:2])  # el prefijo excluye la evidencia de SIRE


async def test_aplicar_copia_actualiza_la_base_y_solo_entonces_borra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origen = f"comprobantes/{RUC}/2026/10/20514831620-01-FF01-00015980.xml"
    destino = f"comprobantes/{RUC}/2026/10/FF01-00015980/20514831620-01-FF01-00015980.xml"
    r2 = _R2([origen])
    conexion = _Conexion(con_fila={origen})
    monkeypatch.setattr(
        "scripts.reorganizar_r2.CloudflareR2StorageProvider", lambda **_: _Proveedor(r2)
    )
    monkeypatch.setattr("scripts.reorganizar_r2.asyncpg", _Asyncpg(conexion))

    copiados, sin_fila, borrados = await aplicar(
        _settings(),
        [Movimiento(origen, destino)],
        {origen},
        borrar_originales=True,
        dsn="postgresql://localhost/prueba",
    )

    assert r2.copiados == [(origen, destino)]
    assert conexion.actualizadas == [(origen, destino)]
    assert r2.borrados == [origen]  # la ruta vieja se borra al final
    assert (copiados, sin_fila, borrados) == (1, 0, 1)
    assert conexion.cerrada
