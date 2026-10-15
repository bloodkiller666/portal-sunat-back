from __future__ import annotations

import types
from uuid import UUID

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.core.config import Settings
from app.core.errors import AuthenticationError, ConfigError
from app.core.security import JwtVerifier
from tests.conftest import JWT_SECRET, USUARIO_ID, crear_token

SECRETO = JWT_SECRET


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "supabase_jwt_secret": SECRETO,
        "supabase_jwt_audience": "authenticated",
    }
    base.update(overrides)
    return Settings(**base)


async def test_token_valido_devuelve_usuario() -> None:
    verifier = JwtVerifier(_settings())
    usuario = await verifier.verify(crear_token())
    assert usuario.id == USUARIO_ID
    assert usuario.email == "usuario@example.com"
    assert usuario.claims_para_rls["sub"] == str(USUARIO_ID)
    assert usuario.claims_para_rls["role"] == "authenticated"


async def test_token_expirado() -> None:
    with pytest.raises(AuthenticationError, match="expiró"):
        await JwtVerifier(_settings()).verify(crear_token(expira_en=-60))


async def test_firma_incorrecta() -> None:
    with pytest.raises(AuthenticationError, match="inválido"):
        await JwtVerifier(_settings()).verify(crear_token(secret="otro-secreto-distinto"))


async def test_audiencia_incorrecta() -> None:
    with pytest.raises(AuthenticationError, match="audiencia"):
        await JwtVerifier(_settings()).verify(crear_token(audience="otra"))


async def test_token_sin_sub() -> None:
    # PyJWT exige `sub` por configuración y lo rechaza antes de nuestra validación.
    with pytest.raises(AuthenticationError):
        await JwtVerifier(_settings()).verify(crear_token(incluir_sub=False))


async def test_token_sin_exp() -> None:
    with pytest.raises(AuthenticationError):
        await JwtVerifier(_settings()).verify(crear_token(incluir_exp=False))


async def test_token_vacio() -> None:
    with pytest.raises(AuthenticationError, match="Falta el token"):
        await JwtVerifier(_settings()).verify("  ")


async def test_sin_configuracion_de_auth() -> None:
    settings = Settings(_env_file=None, supabase_jwt_secret="")
    with pytest.raises(ConfigError, match="SUPABASE_JWT_SECRET"):
        await JwtVerifier(settings).verify(crear_token())


async def test_verifica_emisor_cuando_esta_configurado() -> None:
    settings = _settings(supabase_url="https://demo.supabase.co")
    # Sin claim `iss` el token no es válido cuando el emisor es obligatorio.
    with pytest.raises(AuthenticationError, match="requerido"):
        await JwtVerifier(settings).verify(crear_token())

    # Con un emisor distinto al configurado también se rechaza.
    con_emisor_ajeno = crear_token(issuer="https://otro.example.com/auth/v1")
    with pytest.raises(AuthenticationError, match="emisor"):
        await JwtVerifier(settings).verify(con_emisor_ajeno)


async def test_verifica_token_asimetrico_con_jwks(monkeypatch: pytest.MonkeyPatch) -> None:
    clave_privada = ec.generate_private_key(ec.SECP256R1())
    publica_pem = clave_privada.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    token = jwt.encode(
        {
            "sub": str(USUARIO_ID),
            "aud": "authenticated",
            "role": "authenticated",
            "iss": "https://demo.supabase.co/auth/v1",
            "exp": 4102444800,
        },
        clave_privada,
        algorithm="ES256",
    )

    settings = Settings(
        _env_file=None, supabase_url="https://demo.supabase.co", supabase_jwt_alg="ES256"
    )
    monkeypatch.setattr(
        jwt.PyJWKClient,
        "get_signing_key_from_jwt",
        lambda self, _token: types.SimpleNamespace(key=publica_pem),
    )

    usuario = await JwtVerifier(settings).verify(token)
    assert usuario.id == UUID(str(USUARIO_ID))


async def test_no_acepta_hs256_cuando_se_espera_es256(monkeypatch: pytest.MonkeyPatch) -> None:
    clave_privada = ec.generate_private_key(ec.SECP256R1())
    publica_pem = clave_privada.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    settings = Settings(
        _env_file=None, supabase_url="https://demo.supabase.co", supabase_jwt_alg="ES256"
    )
    monkeypatch.setattr(
        jwt.PyJWKClient,
        "get_signing_key_from_jwt",
        lambda self, _token: types.SimpleNamespace(key=publica_pem),
    )
    # Token HS256 firmado con el mismo material: debe rechazarse por algoritmo.
    with pytest.raises(AuthenticationError):
        await JwtVerifier(settings).verify(crear_token(secret=SECRETO, algoritmo="HS256"))


def _token_con_iat(desfase_segundos: int) -> str:
    import time

    ahora = int(time.time())
    return jwt.encode(
        {
            "sub": str(USUARIO_ID),
            "aud": "authenticated",
            "role": "authenticated",
            "iat": ahora + desfase_segundos,
            "exp": ahora + 3600,
        },
        SECRETO,
        algorithm="HS256",
    )


async def test_un_token_recien_emitido_con_reloj_adelantado_se_acepta() -> None:
    usuario = await JwtVerifier(_settings()).verify(_token_con_iat(5))
    assert usuario.id == USUARIO_ID


async def test_un_token_con_iat_muy_en_el_futuro_se_rechaza() -> None:
    with pytest.raises(AuthenticationError):
        await JwtVerifier(_settings()).verify(_token_con_iat(600))
