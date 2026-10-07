"""Envío de comprobantes por correo (fase posterior)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(slots=True)
class AdjuntoCorreo:
    nombre: str
    contenido: bytes
    mime_type: str


@runtime_checkable
class MailSender(Protocol):
    """Contrato de envío de correo."""

    async def send(
        self,
        *,
        destinatarios: list[str],
        asunto: str,
        cuerpo: str,
        adjuntos: list[AdjuntoCorreo] | None = None,
        copias: list[str] | None = None,
    ) -> None: ...


class SmtpMailSender:
    """Implementación SMTP. No implementada."""

    def __init__(self, *, host: str, puerto: int, usuario: str, password: str) -> None:
        raise NotImplementedError("El envío por correo es de una fase posterior")
