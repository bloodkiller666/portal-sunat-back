from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Entorno, Settings
from app.core.enums import SunatAmbiente

RAIZ = Path(__file__).resolve().parent.parent


def test_valores_por_defecto_apuntan_a_beta() -> None:
    settings = Settings(_env_file=None, database_url="", supabase_jwt_secret="")
    assert settings.sunat_env is SunatAmbiente.beta
    assert settings.confirm_prod_emission is False
    assert settings.environment is Entorno.local
    assert settings.database_configured is False
    assert settings.auth_configured is False
    assert settings.emission_allowed_for() is True


def test_cors_acepta_csv_y_json() -> None:
    assert Settings(_env_file=None, cors_origins="a, b ,c").cors_origins == ["a", "b", "c"]
    assert Settings(_env_file=None, cors_origins='["x","y"]').cors_origins == ["x", "y"]
    assert Settings(_env_file=None, cors_origins="").cors_origins == []
    assert Settings(_env_file=None, cors_allow_methods="GET,POST").cors_allow_methods == [
        "GET",
        "POST",
    ]


def test_campos_opcionales_vacios_quedan_en_none() -> None:
    settings = Settings(
        _env_file=None,
        database_url="",
        supabase_url="",
        supabase_jwt_secret="",
        sunat_wsdl_dir="",
        azure_keyvault_url="",
    )
    assert settings.database_url is None
    assert settings.supabase_url is None
    assert settings.supabase_jwt_secret is None
    assert settings.sunat_wsdl_dir is None
    assert settings.azure_keyvault_url is None


def test_ambiente_global_y_por_empresa() -> None:
    settings = Settings(_env_file=None, sunat_env="prod")
    # Sin confirmación explícita, la emisión en producción está bloqueada.
    assert settings.emission_allowed_for() is False
    assert settings.emission_allowed_for(SunatAmbiente.prod) is False
    # Una empresa puede quedarse en beta mientras el grupo va a producción.
    assert settings.emission_allowed_for(SunatAmbiente.beta) is True

    con_confirmacion = Settings(_env_file=None, sunat_env="prod", confirm_prod_emission=True)
    assert con_confirmacion.emission_allowed_for() is True


def test_sunat_env_solo_admite_beta_o_prod() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, sunat_env="test")


def test_staging_y_prod_exigen_cors_explicito() -> None:
    with pytest.raises(ValidationError, match="CORS_ORIGINS es obligatorio"):
        Settings(_env_file=None, environment="staging")

    with pytest.raises(ValidationError, match="no puede contener"):
        Settings(_env_file=None, environment="staging", cors_origins=["*"])

    settings = Settings(
        _env_file=None, environment="staging", cors_origins=["https://portal.example.com"]
    )
    assert settings.is_production_like is True
    # El comodín sigue permitido en local (el middleware avisa y evita credenciales).
    assert Settings(_env_file=None, environment="local", cors_origins=["*"]).cors_origins == ["*"]


def test_pool_min_max_incoherente() -> None:
    with pytest.raises(ValidationError, match="DATABASE_POOL_MAX_SIZE"):
        Settings(_env_file=None, database_pool_min_size=5, database_pool_max_size=2)


def test_derivados_de_supabase() -> None:
    settings = Settings(
        _env_file=None, supabase_url="https://abc.supabase.co/", supabase_jwt_secret="secreto"
    )
    assert settings.jwks_url == "https://abc.supabase.co/auth/v1/.well-known/jwks.json"
    assert settings.jwt_issuer == "https://abc.supabase.co/auth/v1"
    assert settings.jwt_uses_asymmetric_keys is False
    assert settings.auth_configured is True

    asimetrico = Settings(
        _env_file=None, supabase_url="https://abc.supabase.co", supabase_jwt_alg="ES256"
    )
    assert asimetrico.jwt_uses_asymmetric_keys is True
    assert asimetrico.auth_configured is True


def test_startup_warnings_avisa_de_lo_que_falta() -> None:
    settings = Settings(_env_file=None, database_url="", supabase_jwt_secret="")
    avisos = " ".join(settings.startup_warnings())
    assert "JWT" in avisos
    assert "DATABASE_URL" in avisos

    en_prod = Settings(_env_file=None, sunat_env="prod", database_url="", supabase_jwt_secret="")
    assert "CONFIRM_PROD_EMISSION=false" in " ".join(en_prod.startup_warnings())


def test_env_example_documenta_todas_las_variables() -> None:
    contenido = (RAIZ / ".env.example").read_text(encoding="utf-8")
    faltantes = [campo for campo in Settings.model_fields if campo.upper() not in contenido]
    assert faltantes == [], f"Variables sin documentar en .env.example: {faltantes}"


def test_env_example_no_incluye_valores_secretos() -> None:
    contenido = (RAIZ / ".env.example").read_text(encoding="utf-8")
    assert "SUNAT_CRED_20123456789_SOL_USER" in contenido
    # Las claves (SOL, certificado y API REST) deben quedar siempre vacías.
    for variable in (
        "SUNAT_CRED_20123456789_SOL_PASSWORD",
        "SUNAT_CRED_20123456789_CERT_PASSWORD",
        "SUNAT_CRED_20123456789_CLIENT_SECRET",
    ):
        assert re.search(rf"^{variable}=$", contenido, re.MULTILINE), (
            f"{variable} no debería traer valor en .env.example"
        )


def test_gitignore_excluye_env_y_secrets() -> None:
    gitignore = (RAIZ / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in gitignore
    assert "secrets/" in gitignore
    assert "*.pfx" in gitignore
    assert "!.env.example" in gitignore


def test_las_credenciales_del_env_file_llegan_al_entorno_sin_pisar_el_real(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core import config

    ruta = tmp_path / ".env"
    ruta.write_text(
        "SUNAT_CRED_20123456789_SOL_USER=DESDEARCHIVO\n"
        "SUNAT_CRED_20123456789_SOL_PASSWORD=clave-archivo\n"
        "OTRA_VARIABLE=no-debe-exportarse\n",
        encoding="utf-8",
    )
    entorno = {"SUNAT_CRED_20123456789_SOL_PASSWORD": "clave-del-entorno-real"}
    monkeypatch.setattr(config.os, "environ", entorno)

    config._exportar_credenciales_del_env_file(str(ruta))

    assert entorno["SUNAT_CRED_20123456789_SOL_USER"] == "DESDEARCHIVO"
    # El entorno real (Docker/nube) siempre manda sobre el archivo.
    assert entorno["SUNAT_CRED_20123456789_SOL_PASSWORD"] == "clave-del-entorno-real"
    assert "OTRA_VARIABLE" not in entorno


def test_sin_env_file_no_pasa_nada(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import config

    entorno: dict[str, str] = {}
    monkeypatch.setattr(config.os, "environ", entorno)

    config._exportar_credenciales_del_env_file(str(tmp_path / "no-existe.env"))

    assert entorno == {}
