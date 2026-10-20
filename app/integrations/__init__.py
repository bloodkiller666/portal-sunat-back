"""Integraciones preparadas pero no implementadas (fuera de alcance de la fase).

Cada módulo define el Protocol que consumirán los servicios cuando se habilite la
integración correspondiente. Ninguna implementa lógica real todavía.
"""

from app.integrations.mailer import MailSender, SmtpMailSender
from app.integrations.sap import SapClient, SapSoapClient
from app.integrations.xml_signer import SunatXmlSigner, XmlSigner

__all__ = [
    "MailSender",
    "SapClient",
    "SapSoapClient",
    "SmtpMailSender",
    "SunatXmlSigner",
    "XmlSigner",
]
