"""Modelos de respuesta de los servicios de SUNAT."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TramasSOAP(BaseModel):
    """Tramas y metadatos de una invocación. `request_xml` nunca lleva credenciales."""

    model_config = ConfigDict(extra="ignore")

    endpoint: str
    metodo: str
    request_xml: str | None = None
    response_xml: str | None = None
    http_status: int | None = None
    duracion_ms: int | None = None


class CdrInfo(BaseModel):
    """Contenido interpretado del CDR (ZIP base64 -> ApplicationResponse)."""

    model_config = ConfigDict(extra="ignore")

    codigo: str
    descripcion: str
    notas: list[str] = Field(default_factory=list)
    archivo: str | None = None
    identificador: str | None = None
    fecha_respuesta: datetime | None = None
    xml: str | None = None

    @property
    def aceptado(self) -> bool:
        return self.codigo.strip() == "0"


class EstadoSunat(BaseModel):
    """Resultado de `getStatus` / `getStatusCdr`."""

    model_config = ConfigDict(extra="ignore")

    codigo: str | None = None
    mensaje: str | None = None
    tiene_cdr: bool = False


class ResultadoConsulta(BaseModel):
    """Resultado de una consulta de estado/CDR lista para registrar y responder."""

    model_config = ConfigDict(extra="ignore")

    estado: EstadoSunat
    cdr: CdrInfo | None = None
    error_cdr: str | None = None
    tramas: TramasSOAP


class ResultadoDisponibilidad(BaseModel):
    """Resultado de la prueba de conexión y credenciales."""

    model_config = ConfigDict(extra="ignore")

    autenticado: bool = False
    metodo: str | None = None
    endpoint: str | None = None
    codigo: str | None = None
    mensaje: str | None = None
    http_status: int | None = None
    duracion_ms: int | None = None
    request_xml: str | None = None
    response_xml: str | None = None
    error_tipo: str | None = None
