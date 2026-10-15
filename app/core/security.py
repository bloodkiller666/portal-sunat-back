"""Verificación de los JWT emitidos por Supabase Auth (regla 7).

Soporta los dos esquemas de firma de Supabase:

* `HS256/HS384/HS512` con el JWT secret del proyecto (proyectos legacy).
* `ES256/ES384/RS256` con las signing keys asimétricas, validando contra el JWKS.
"""

from __future__ import annotations

import threading
from typing import Any
from uuid import UUID

import anyio
import jwt
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError
from pydantic import BaseModel, Field

from app.core.config import Settings
from app.core.errors import AuthenticationError, ConfigError
from app.core.logging import get_logger

logger = get_logger(__name__)

# Tolerancia al desfase entre el reloj de Supabase y el del servidor: sin ella, un token
# recién emitido (`iat` unos segundos adelante) se rechazaba de forma intermitente.
LEEWAY_RELOJ_SEGUNDOS = 30


class AuthenticatedUser(BaseModel):
    """Usuario autenticado extraído del JWT."""

    id: UUID
    email: str | None = None
    role: str = "authenticated"
    claims: dict[str, Any] = Field(default_factory=dict)

    @property
    def claims_para_rls(self) -> dict[str, Any]:
        """Claims que se inyectan en la sesión de Postgres para que RLS evalúe auth.uid()."""
        return {"sub": str(self.id), "role": self.role, **self.claims}


class JwtVerifier:
    """Valida el token y expone el usuario resultante."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._jwks_client: jwt.PyJWKClient | None = None
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- #
    # API
    # ---------------------------------------------------------------- #
    async def verify(self, token: str) -> AuthenticatedUser:
        token = (token or "").strip()
        if not token:
            raise AuthenticationError("Falta el token de acceso")

        payload = await anyio.to_thread.run_sync(self._decode, token)

        subject = payload.get("sub")
        if not subject:
            raise AuthenticationError("El token no contiene el identificador del usuario")
        try:
            user_id = UUID(str(subject))
        except ValueError as exc:
            raise AuthenticationError("El identificador del usuario no es un UUID válido") from exc

        return AuthenticatedUser(
            id=user_id,
            email=payload.get("email"),
            role=str(payload.get("role") or "authenticated"),
            claims=payload,
        )

    # ---------------------------------------------------------------- #
    # Internos
    # ---------------------------------------------------------------- #
    def _jwks(self) -> jwt.PyJWKClient:
        with self._lock:
            if self._jwks_client is None:
                url = self._settings.jwks_url
                if not url:
                    raise ConfigError(
                        "No hay JWKS configurado (SUPABASE_URL o SUPABASE_JWT_JWKS_URL)"
                    )
                self._jwks_client = jwt.PyJWKClient(url, cache_keys=True, timeout=10)
            return self._jwks_client

    def _decode(self, token: str) -> dict[str, Any]:
        settings = self._settings
        if not settings.auth_configured:
            raise ConfigError(
                "La verificación de JWT no está configurada: defina SUPABASE_JWT_SECRET "
                "(HS256) o SUPABASE_JWT_JWKS_URL/SUPABASE_URL (ES256/RS256)"
            )

        audience = settings.supabase_jwt_audience or None
        issuer = settings.jwt_issuer
        options = {
            "require": ["exp", "sub"],
            "verify_aud": audience is not None,
            "verify_iss": issuer is not None,
        }

        try:
            if settings.jwt_uses_asymmetric_keys:
                signing_key = self._jwks().get_signing_key_from_jwt(token)
                key: Any = signing_key.key
            else:
                key = settings.jwt_secret_value

            return jwt.decode(
                token,
                key,
                algorithms=[settings.supabase_jwt_alg],
                audience=audience,
                issuer=issuer,
                options=options,
                leeway=LEEWAY_RELOJ_SEGUNDOS,
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationError("El token expiró") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise AuthenticationError(
                f"El token no contiene el claim requerido '{exc.claim}'"
            ) from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthenticationError("La audiencia del token no es válida") from exc
        except jwt.InvalidIssuerError as exc:
            raise AuthenticationError("El emisor del token no es válido") from exc
        except (PyJWKClientError, PyJWKClientConnectionError) as exc:
            logger.warning("No se pudo obtener la clave pública del JWKS: %s", exc)
            raise AuthenticationError("No se pudo validar la firma del token") from exc
        except jwt.InvalidTokenError as exc:
            raise AuthenticationError("Token inválido") from exc
