"""Dobles en memoria y constructores de datos para las pruebas (regla 8: sin SUNAT real)."""

from __future__ import annotations

import base64
import io
import zipfile
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from app.core.enums import EstadoComprobante, RolEmpresa, SunatAmbiente
from app.core.errors import NotFoundError
from app.db.models import (
    Archivo,
    Company,
    CompanyMember,
    Comprobante,
    Membership,
    SunatEnvio,
    SunatEnvioRegistro,
)
from app.sunat.models import (
    CdrInfo,
    EstadoSunat,
    ResultadoConsulta,
    ResultadoDisponibilidad,
    TramasSOAP,
)

FIXTURES = Path(__file__).parent / "fixtures"
CDR_DIR = FIXTURES / "cdr"
WSDL_DIR = FIXTURES / "wsdl"

ENDPOINT_CONSULTA = "https://sunat.test/billConsultService"
ENDPOINT_BILL = "https://sunat.test/billService"


# --------------------------------------------------------------------------- #
# Constructores de datos
# --------------------------------------------------------------------------- #
def construir_empresa(
    *,
    ruc: str = "20123456789",
    razon_social: str = "ACME SAC",
    ambiente: SunatAmbiente | None = None,
    empresa_id: UUID | None = None,
) -> Company:
    return Company(
        id=empresa_id or uuid4(),
        ruc=ruc,
        razon_social=razon_social,
        nombre_comercial=None,
        activo=True,
        sunat_ambiente=ambiente,
    )


def construir_membresia(
    user_id: UUID, company_id: UUID, rol: RolEmpresa = RolEmpresa.emisor
) -> Membership:
    return Membership(user_id=user_id, company_id=company_id, rol=rol, activo=True)


def construir_comprobante(
    company_id: UUID,
    *,
    ruc: str = "20123456789",
    tipo_documento: str = "01",
    serie: str = "F001",
    correlativo: int = 1,
    estado: EstadoComprobante = EstadoComprobante.pendiente,
    ambiente: SunatAmbiente = SunatAmbiente.beta,
    monto_total: Decimal | None = Decimal("118.00"),
    comprobante_id: UUID | None = None,
) -> Comprobante:
    numero = f"{correlativo:08d}"
    return Comprobante(
        id=comprobante_id or uuid4(),
        company_id=company_id,
        ruc_emisor=ruc,
        tipo_documento=tipo_documento,
        serie=serie,
        correlativo=correlativo,
        numero=numero,
        nombre_archivo=f"{ruc}-{tipo_documento}-{serie}-{numero}",
        moneda="PEN",
        monto_total=monto_total,
        estado=estado,
        sunat_ambiente=ambiente,
    )


def cargar_cdr_xml(nombre: str) -> str:
    return (CDR_DIR / nombre).read_text(encoding="utf-8")


def empaquetar_cdr_zip(xml: str) -> bytes:
    """Construye el ZIP que SUNAT devuelve dentro del CDR."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archivo:
        archivo.writestr("R-20123456789-01-F001-1.xml", xml)
    return buffer.getvalue()


def cdr_base64(xml: str | None = None) -> str:
    return base64.b64encode(empaquetar_cdr_zip(xml or cargar_cdr_xml("cdr_aceptado.xml"))).decode()


# --------------------------------------------------------------------------- #
# Repositorios en memoria
# --------------------------------------------------------------------------- #
@dataclass
class InMemoryCompaniesRepository:
    empresas: list[Company] = field(default_factory=list)
    membresias: list[Membership] = field(default_factory=list)

    async def list_for_user(self, user_id: UUID) -> list[CompanyMember]:
        resultado: list[CompanyMember] = []
        for membresia in self.membresias:
            if membresia.user_id != user_id or not membresia.activo:
                continue
            empresa = next((e for e in self.empresas if e.id == membresia.company_id), None)
            if empresa is not None and empresa.activo:
                resultado.append(CompanyMember(company=empresa, rol=membresia.rol))
        return sorted(resultado, key=lambda m: m.company.razon_social)

    async def get_by_ruc(self, ruc: str) -> Company | None:
        return next((e for e in self.empresas if e.ruc == ruc), None)

    async def get_by_id(self, company_id: UUID) -> Company | None:
        return next((e for e in self.empresas if e.id == company_id), None)

    async def get_membership(self, user_id: UUID, company_id: UUID) -> Membership | None:
        return next(
            (
                m
                for m in self.membresias
                if m.user_id == user_id and m.company_id == company_id and m.activo
            ),
            None,
        )


@dataclass
class InMemoryComprobantesRepository:
    """`company_ids_visibles` emula lo que haría RLS en Postgres."""

    comprobantes: dict[UUID, Comprobante] = field(default_factory=dict)
    company_ids_visibles: set[UUID] = field(default_factory=set)
    actualizaciones: list[Comprobante] = field(default_factory=list)

    async def get(self, comprobante_id: UUID) -> Comprobante | None:
        comprobante = self.comprobantes.get(comprobante_id)
        if comprobante is None or comprobante.company_id not in self.company_ids_visibles:
            return None
        return comprobante

    async def update_sunat_state(
        self,
        comprobante_id: UUID,
        *,
        estado: EstadoComprobante,
        cdr_codigo: str | None,
        cdr_descripcion: str | None,
        cdr_notas: list[str],
    ) -> Comprobante:
        comprobante = self.comprobantes.get(comprobante_id)
        if comprobante is None:
            raise NotFoundError("El comprobante no existe")
        actualizado = comprobante.model_copy(
            update={
                "estado": estado,
                # El SQL real usa coalesce: los valores None conservan lo anterior.
                "cdr_codigo": cdr_codigo or comprobante.cdr_codigo,
                "cdr_descripcion": cdr_descripcion or comprobante.cdr_descripcion,
                "cdr_notas": cdr_notas or comprobante.cdr_notas,
                "ultimo_envio_at": datetime.now(tz=UTC),
            }
        )
        self.comprobantes[comprobante_id] = actualizado
        self.actualizaciones.append(actualizado)
        return actualizado


@dataclass
class InMemorySunatEnviosRepository:
    envios: list[SunatEnvio] = field(default_factory=list)
    fallar: bool = False

    async def record(self, registro: SunatEnvioRegistro) -> SunatEnvio:
        if self.fallar:
            raise RuntimeError("bitácora no disponible (simulado)")
        envio = SunatEnvio(
            id=len(self.envios) + 1,
            created_at=datetime.now(tz=UTC),
            **registro.model_dump(),
        )
        self.envios.append(envio)
        return envio

    async def list_for_comprobante(
        self, comprobante_id: UUID, *, limit: int = 20
    ) -> list[SunatEnvio]:
        coincidencias = [e for e in self.envios if e.comprobante_id == comprobante_id]
        return list(reversed(coincidencias))[:limit]


@dataclass
class InMemoryArchivosRepository:
    archivos: list[Archivo] = field(default_factory=list)

    async def list_for_comprobante(self, comprobante_id: UUID) -> list[Archivo]:
        return [a for a in self.archivos if a.comprobante_id == comprobante_id]


@dataclass
class FakeUnitOfWork:
    companies: InMemoryCompaniesRepository = field(default_factory=InMemoryCompaniesRepository)
    comprobantes: InMemoryComprobantesRepository = field(
        default_factory=InMemoryComprobantesRepository
    )
    sunat_envios: InMemorySunatEnviosRepository = field(
        default_factory=InMemorySunatEnviosRepository
    )
    archivos: InMemoryArchivosRepository = field(default_factory=InMemoryArchivosRepository)
    claims: dict[str, Any] = field(default_factory=dict)


def uow_factory(uow: FakeUnitOfWork):
    """Fábrica de UoW que captura los claims para poder verificarlos en las pruebas."""

    @asynccontextmanager
    async def factory(claims: Mapping[str, Any]) -> AsyncIterator[FakeUnitOfWork]:
        uow.claims = dict(claims)
        yield uow

    return factory


# --------------------------------------------------------------------------- #
# Doble del cliente SUNAT
# --------------------------------------------------------------------------- #
@dataclass
class FakeSunatClient:
    """Cliente SUNAT simulado. Se configura por prueba con `resultado` o `error`."""

    resultado: ResultadoConsulta | None = None
    error: Exception | None = None
    disponibilidad: ResultadoDisponibilidad | None = None
    error_disponibilidad: Exception | None = None
    credentials: Any = None
    env: SunatAmbiente | None = None
    last_tramas: TramasSOAP | None = None
    llamadas: list[dict[str, Any]] = field(default_factory=list)

    def get_status_cdr(self, **kwargs: Any) -> ResultadoConsulta:
        self.llamadas.append({"metodo": "get_status_cdr", **kwargs})
        if self.error is not None:
            raise self.error
        assert self.resultado is not None
        return self.resultado

    def get_status(self, **kwargs: Any) -> ResultadoConsulta:
        self.llamadas.append({"metodo": "get_status", **kwargs})
        if self.error is not None:
            raise self.error
        assert self.resultado is not None
        return self.resultado

    def check_availability(self) -> ResultadoDisponibilidad:
        self.llamadas.append({"metodo": "check_availability"})
        if self.error_disponibilidad is not None:
            raise self.error_disponibilidad
        assert self.disponibilidad is not None
        return self.disponibilidad


def fabricar_cliente_sunat(cliente: FakeSunatClient):
    """Fábrica compatible con `app.state.sunat_client_factory`."""

    def factory(credentials: Any, env: SunatAmbiente | None = None) -> FakeSunatClient:
        cliente.credentials = credentials
        cliente.env = env
        return cliente

    return factory


# --------------------------------------------------------------------------- #
# Constructores de resultados SUNAT
# --------------------------------------------------------------------------- #
def tramas(
    *,
    metodo: str = "getStatusCdr",
    endpoint: str = ENDPOINT_CONSULTA,
    request_xml: str = "<Envelope><!-- sin credenciales --></Envelope>",
    response_xml: str = "<Envelope><!-- respuesta --></Envelope>",
    http_status: int | None = 200,
    duracion_ms: int | None = 42,
) -> TramasSOAP:
    return TramasSOAP(
        endpoint=endpoint,
        metodo=metodo,
        request_xml=request_xml,
        response_xml=response_xml,
        http_status=http_status,
        duracion_ms=duracion_ms,
    )


def resultado_con_cdr(
    xml: str | None = None,
    *,
    codigo: str | None = None,
    mensaje: str | None = None,
) -> ResultadoConsulta:
    info = CdrInfo.model_validate(
        {
            "codigo": codigo or "0",
            "descripcion": mensaje or "La Factura numero F001-00000001, ha sido aceptada",
            "notas": ["El comprobante fue aceptado con observaciones"],
            "archivo": "R-20123456789-01-F001-00000001.xml",
        }
    )
    return ResultadoConsulta(
        estado=EstadoSunat(codigo=info.codigo, mensaje=info.descripcion, tiene_cdr=True),
        cdr=info,
        tramas=tramas(),
    )


def resultado_sin_cdr(codigo: str, mensaje: str) -> ResultadoConsulta:
    return ResultadoConsulta(
        estado=EstadoSunat(codigo=codigo, mensaje=mensaje, tiene_cdr=False),
        cdr=None,
        tramas=tramas(metodo="getStatusCdr"),
    )


def disponibilidad_autenticado() -> ResultadoDisponibilidad:
    return ResultadoDisponibilidad(
        autenticado=True,
        metodo="getStatus",
        endpoint=ENDPOINT_BILL,
        codigo="0",
        mensaje="El comprobante no existe",
        http_status=200,
        duracion_ms=35,
        request_xml="<Envelope><!-- sin credenciales --></Envelope>",
        response_xml="<Envelope><!-- respuesta --></Envelope>",
    )


def disponibilidad_credenciales_invalidas() -> ResultadoDisponibilidad:
    return ResultadoDisponibilidad(
        autenticado=False,
        metodo="getStatus",
        endpoint=ENDPOINT_BILL,
        codigo="0104",
        mensaje="La clave ingresada es incorrecta",
        http_status=401,
        duracion_ms=30,
        error_tipo="sunat_auth_error",
    )


def disponibilidad_servicio_caido() -> ResultadoDisponibilidad:
    return ResultadoDisponibilidad(
        autenticado=False,
        metodo="getStatus",
        endpoint=ENDPOINT_BILL,
        mensaje="No se pudo contactar a SUNAT (timeout o error de red)",
        duracion_ms=20000,
        error_tipo="sunat_unavailable",
    )
