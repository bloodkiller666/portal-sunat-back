"""Firma digital del XML UBL 2.1 (fase posterior).

`signxml` ya está en las dependencias para no cambiar el entorno cuando se
habilite esta parte. La firma usa el certificado de la empresa obtenido del
`SecretsProvider` (nunca del repositorio).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.secrets.base import CompanyCredentials


@runtime_checkable
class XmlSigner(Protocol):
    """Contrato de firma de comprobantes electrónicos."""

    def sign(self, *, xml: bytes, credentials: CompanyCredentials) -> bytes: ...


class SunatXmlSigner:
    """Firma XMLDSig con el certificado de la empresa. No implementada."""

    def __init__(self, *, credentials: CompanyCredentials) -> None:
        raise NotImplementedError(
            "La firma del XML es de una fase posterior: primero se implementa la emisión"
        )
