"""Redacción de información sensible en logs (regla 2).

Dos mecanismos complementarios:

1. `register_secret_values`: valores conocidos (clave SOL, JWT secret, contraseña
   del certificado) que se reemplazan literalmente si aparecen en un mensaje.
2. Patrones: cubren los casos en los que el secreto no se registró (por ejemplo el
   XML de WS-Security, un header Authorization o un payload base64).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

REDACTED = "«redactado»"

_MASK = REDACTED


def _mask_tag(match: re.Match[str]) -> str:
    return f"{match.group(1)}{_MASK}{match.group(3)}"


def _mask_pair(match: re.Match[str]) -> str:
    return f"{match.group(1)}={_MASK}"


def _mask_prefix(match: re.Match[str]) -> str:
    return f"{match.group(1)}{_MASK}"


def _mask_everything(_match: re.Match[str]) -> str:
    return _MASK


_PATTERNS: tuple[tuple[re.Pattern[str], object], ...] = (
    # <ns2:Password Type="...">clave</ns2:Password>
    (
        re.compile(r"(?is)(<(?:\w+:)?Password\b[^>]*>)(.*?)(</(?:\w+:)?Password>)"),
        _mask_tag,
    ),
    # El header completo se reemplaza: es el que transporta la clave SOL.
    (
        re.compile(r"(?is)<(?:\w+:)?Security\b[^>]*>.*?</(?:\w+:)?Security>"),
        _mask_everything,
    ),
    (
        re.compile(
            r"(?is)(<(?:[\w]+:)?BinarySecurityToken\b[^>]*>)(.*?)(</(?:[\w]+:)?BinarySecurityToken>)"
        ),
        _mask_tag,
    ),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), _mask_prefix),
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.S,
        ),
        _mask_everything,
    ),
    (
        re.compile(r"data:application/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+", re.I),
        _mask_everything,
    ),
)

# Claves y separadores: cubre `clave=valor`, `"clave_sol": "valor"` y
# `Authorization: Bearer xxx`, tanto en texto plano como en JSON.
_KV_PATTERN = re.compile(
    r"(?i)(?P<clave>password|passwd|clave|clave_sol|secret|client_secret|secret_key|token"
    r"|access_token|refresh_token|api_key|apikey|authorization|sol_password)"
    r"(?P<sep>[\"']?\s*[:=]\s*[\"']?)"
    r"(?P<valor>[^\s\"',;)\]}]+)"
)

_registered_secrets: set[str] = set()


def register_secret_values(*values: object) -> None:
    """Registra valores concretos que deben quedar fuera de los logs.

    Se llama cuando se resuelven credenciales de una empresa o al cargar la
    configuración. Acepta objetos `SecretStr`/`SecretBytes` y cadenas.
    """
    for value in values:
        raw = _unwrap(value)
        if raw:
            _registered_secrets.add(raw)


def clear_registered_secrets() -> None:
    _registered_secrets.clear()


def _unwrap(value: object) -> str | None:
    if value is None:
        return None
    getter = getattr(value, "get_secret_value", None)
    if callable(getter):
        value = getter()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    if isinstance(value, str):
        return value
    return None


def redact(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Devuelve el texto con los secretos conocidos y los patrones enmascarados."""
    if not text:
        return text

    result = text
    candidates = set(_registered_secrets)
    for candidate in extra_secrets:
        raw = _unwrap(candidate)
        if raw:
            candidates.add(raw)

    # Solo se reemplazan valores con cuerpo suficiente para evitar ruido.
    for secret in sorted(candidates, key=len, reverse=True):
        if len(secret) >= 4:
            result = result.replace(secret, _MASK)

    for pattern, replacement in _PATTERNS:
        if replacement is None:
            continue
        result = pattern.sub(replacement, result)  # type: ignore[arg-type]

    result = _KV_PATTERN.sub(
        lambda m: f"{m.group('clave')}{m.group('sep')}{_MASK}",
        result,
    )
    return result


class RedactionFilter(logging.Filter):
    """Filtro de logging que enmascara secretos antes de emitir el registro."""

    def __init__(self, extra_secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._extra_secrets = tuple(extra_secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - mensajes mal formados
            return True

        redacted = redact(message, self._extra_secrets)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True
