"""Enumeraciones compartidas por las capas de dominio, base de datos y API."""

from __future__ import annotations

from enum import StrEnum


class SunatAmbiente(StrEnum):
    """Ambiente de SUNAT. `beta` es solo para pruebas, `prod` es real."""

    beta = "beta"
    prod = "prod"


class RolEmpresa(StrEnum):
    """Roles dentro de una empresa (tabla `user_companies`)."""

    admin = "admin"
    emisor = "emisor"
    lector = "lector"


ROLES_SOLO_LECTURA = frozenset({RolEmpresa.lector})
ROLES_OPERATIVOS = frozenset({RolEmpresa.admin, RolEmpresa.emisor})


class EstadoComprobante(StrEnum):
    """Estados que consume el frontend (siempre en minúsculas en la API)."""

    pendiente = "pendiente"
    aprobado = "aprobado"
    rechazado = "rechazado"
    anulado = "anulado"

    @property
    def etiqueta(self) -> str:
        """Etiqueta legible por el frontend: Aprobado, Rechazado, Pendiente, Anulado."""
        return _ETIQUETAS_ESTADO[self]


_ETIQUETAS_ESTADO: dict[EstadoComprobante, str] = {
    EstadoComprobante.pendiente: "Pendiente",
    EstadoComprobante.aprobado: "Aprobado",
    EstadoComprobante.rechazado: "Rechazado",
    EstadoComprobante.anulado: "Anulado",
}


class OrigenComprobante(StrEnum):
    """De quién es el comprobante dentro del portal.

    `emitido` es de la empresa (el emisor es su RUC); `recibido` es una compra a
    un tercero, que se archiva para el registro de compras.
    """

    emitido = "emitido"
    recibido = "recibido"

    @property
    def etiqueta(self) -> str:
        return _ETIQUETAS_ORIGEN[self]


_ETIQUETAS_ORIGEN: dict[OrigenComprobante, str] = {
    OrigenComprobante.emitido: "Emitido",
    OrigenComprobante.recibido: "Recibido",
}


class TipoOperacionSunat(StrEnum):
    """Tipo de interacción con SUNAT que se registra en `sunat_envios`."""

    envio = "envio"
    consulta_estado = "consulta_estado"
    consulta_cdr = "consulta_cdr"
    prueba_conexion = "prueba_conexion"
    comunicacion_baja = "comunicacion_baja"
    resumen_diario = "resumen_diario"
    reversion = "reversion"


class TipoArchivo(StrEnum):
    pdf = "pdf"
    xml = "xml"
    cdr = "cdr"
    zip = "zip"


class EstadoArchivo(StrEnum):
    pendiente = "pendiente"
    subido = "subido"
    error = "error"
    eliminado = "eliminado"


class RespaldoFiltro(StrEnum):
    """Filtro por cobertura de respaldo (archivos subidos a R2) para preparar auditorías."""

    sin_xml = "sin_xml"
    sin_cdr = "sin_cdr"
    completo = "completo"
    # Compras (XML + PDF, sin CDR): qué combinación de los dos archivos tiene la compra.
    solo_xml = "solo_xml"
    solo_pdf = "solo_pdf"
    xml_pdf = "xml_pdf"
    ninguno = "ninguno"


class CorreoFiltro(StrEnum):
    """«Estado correo» de una compra: con el asunto de su correo cargado o sin él."""

    enviado = "enviado"
    no_enviado = "no_enviado"


class CampoOrden(StrEnum):
    """Columnas por las que se puede ordenar el listado (lista blanca contra SQL injection)."""

    fecha = "fecha"
    numero = "numero"
    cliente = "cliente"
    importe = "importe"


class DireccionOrden(StrEnum):
    asc = "asc"
    desc = "desc"
