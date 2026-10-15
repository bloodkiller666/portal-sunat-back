"""Dobles en memoria y constructores de datos para las pruebas (regla 8: sin SUNAT real)."""

from __future__ import annotations

import base64
import hashlib
import io
import zipfile
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from app.core.enums import (
    EstadoComprobante,
    OrigenComprobante,
    RolEmpresa,
    SunatAmbiente,
)
from app.core.errors import ConflictError, NotFoundError
from app.db.models import (
    Archivo,
    ArchivoRegistro,
    Company,
    CompanyMember,
    Comprobante,
    ComprobanteItem,
    ComprobanteItemRegistro,
    ComprobanteRegistro,
    Membership,
    SunatEnvio,
    SunatEnvioRegistro,
)
from app.storage.base import FileUploadResult
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
    fecha_emision: date | None = None,
    origen: OrigenComprobante = OrigenComprobante.emitido,
    comprobante_id: UUID | None = None,
) -> Comprobante:
    numero = f"{correlativo:08d}"
    return Comprobante(
        origen=origen,
        fecha_emision=fecha_emision,
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

    async def list_for_company(
        self,
        company_id: UUID,
        *,
        estado: EstadoComprobante | None = None,
        origen: OrigenComprobante | None = None,
        fecha_desde: date | None = None,
        fecha_hasta: date | None = None,
        tipo_documento: str | None = None,
        serie: str | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Comprobante]:
        if company_id not in self.company_ids_visibles:
            return []
        visibles = [c for c in self.comprobantes.values() if c.company_id == company_id]
        if estado is not None:
            visibles = [c for c in visibles if c.estado is estado]
        if origen is not None:
            visibles = [c for c in visibles if c.origen is origen]
        if fecha_desde is not None:
            visibles = [c for c in visibles if (c.fecha_emision or date.min) >= fecha_desde]
        if fecha_hasta is not None:
            visibles = [c for c in visibles if (c.fecha_emision or date.min) <= fecha_hasta]
        if tipo_documento is not None:
            visibles = [c for c in visibles if c.tipo_documento == tipo_documento]
        if serie is not None:
            visibles = [c for c in visibles if c.serie == serie]
        if q is not None:
            # Equivale al `ilike %q%` de Postgres sobre `nombre_archivo`.
            visibles = [c for c in visibles if q.lower() in (c.nombre_archivo or "").lower()]
        visibles.sort(
            key=lambda c: (c.fecha_emision or date.min, c.correlativo), reverse=True
        )
        return visibles[offset : offset + limit]

    async def create(self, registro: ComprobanteRegistro) -> Comprobante:
        duplicado = next(
            (
                c
                for c in self.comprobantes.values()
                if c.company_id == registro.company_id
                and c.origen is registro.origen
                and c.tipo_documento == registro.tipo_documento
                and c.serie == registro.serie
                and c.correlativo == registro.correlativo
                and c.sunat_ambiente == registro.sunat_ambiente
            ),
            None,
        )
        if duplicado is not None:
            raise ConflictError(
                "Ese comprobante ya está registrado para la empresa en el mismo ambiente"
            )
        creado = Comprobante(id=uuid4(), **registro.model_dump())
        self.comprobantes[creado.id] = creado
        return creado

    def _buscar(
        self,
        company_id: UUID,
        *,
        origen: OrigenComprobante,
        tipo_documento: str,
        serie: str,
        correlativo: int,
        sunat_ambiente: SunatAmbiente,
    ) -> Comprobante | None:
        return next(
            (
                c
                for c in self.comprobantes.values()
                if c.company_id == company_id
                and c.origen is origen
                and c.tipo_documento == tipo_documento
                and c.serie == serie
                and c.correlativo == correlativo
                and c.sunat_ambiente == sunat_ambiente
            ),
            None,
        )

    async def find_by_key(
        self,
        company_id: UUID,
        *,
        origen: OrigenComprobante,
        tipo_documento: str,
        serie: str,
        correlativo: int,
        sunat_ambiente: SunatAmbiente,
    ) -> Comprobante | None:
        return self._buscar(
            company_id,
            origen=origen,
            tipo_documento=tipo_documento,
            serie=serie,
            correlativo=correlativo,
            sunat_ambiente=sunat_ambiente,
        )

    async def create_if_absent(self, registro: ComprobanteRegistro) -> Comprobante | None:
        existente = self._buscar(
            registro.company_id,
            origen=registro.origen,
            tipo_documento=registro.tipo_documento,
            serie=registro.serie,
            correlativo=registro.correlativo,
            sunat_ambiente=registro.sunat_ambiente,
        )
        if existente is not None:
            return None
        return await self.create(registro)

    async def completar_desde_xml(
        self, comprobante_id: UUID, registro: ComprobanteRegistro
    ) -> Comprobante:
        comprobante = self.comprobantes.get(comprobante_id)
        if comprobante is None:
            raise NotFoundError("El comprobante no existe")
        campos = (
            "ruc_receptor",
            "receptor_doc",
            "razon_social_emisor",
            "razon_social_receptor",
            "nombre_archivo",
            "fecha_emision",
            "fecha_vencimiento",
            "forma_pago",
            "moneda",
            "monto_total",
        )
        cambios = {campo: getattr(registro, campo) for campo in campos}
        actualizado = comprobante.model_copy(update={**cambios, "fuente": "xml"})
        self.comprobantes[comprobante_id] = actualizado
        return actualizado

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

    async def update_sap(
        self,
        comprobante_id: UUID,
        *,
        check_sap: bool | None = None,
        n_sap: str | None = None,
        cambiar_n_sap: bool = False,
    ) -> Comprobante:
        comprobante = self.comprobantes.get(comprobante_id)
        if comprobante is None:
            raise NotFoundError("El comprobante no existe")
        cambios: dict[str, object] = {}
        if check_sap is not None:
            cambios["check_sap"] = check_sap
        if cambiar_n_sap:
            cambios["n_sap"] = n_sap
        actualizado = comprobante.model_copy(update=cambios)
        self.comprobantes[comprobante_id] = actualizado
        return actualizado


@dataclass
class InMemoryComprobanteItemsRepository:
    items: list[ComprobanteItem] = field(default_factory=list)

    async def create_many(
        self, registros: list[ComprobanteItemRegistro]
    ) -> list[ComprobanteItem]:
        ahora = datetime.now(tz=UTC)
        creados = [
            ComprobanteItem(
                id=uuid4(),
                created_at=ahora,
                updated_at=ahora,
                **registro.model_dump(),
            )
            for registro in registros
        ]
        self.items.extend(creados)
        return creados

    async def list_for_comprobante(self, comprobante_id: UUID) -> list[ComprobanteItem]:
        coincidencias = [i for i in self.items if i.comprobante_id == comprobante_id]
        return sorted(coincidencias, key=lambda i: i.item_nro)


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
    """`company_ids_visibles` emula lo que haría RLS en Postgres."""

    archivos: list[Archivo] = field(default_factory=list)
    company_ids_visibles: set[UUID] = field(default_factory=set)

    async def get(self, archivo_id: UUID) -> Archivo | None:
        archivo = next((a for a in self.archivos if a.id == archivo_id), None)
        if archivo is None or archivo.company_id not in self.company_ids_visibles:
            return None
        return archivo

    async def list_for_comprobante(self, comprobante_id: UUID) -> list[Archivo]:
        return [a for a in self.archivos if a.comprobante_id == comprobante_id]

    async def registrar(self, registro: ArchivoRegistro) -> Archivo:
        archivo = Archivo(
            id=uuid4(),
            subido_at=datetime.now(tz=UTC),
            **registro.model_dump(),
        )
        self.archivos.append(archivo)
        return archivo


@dataclass
class FakeUnitOfWork:
    companies: InMemoryCompaniesRepository = field(default_factory=InMemoryCompaniesRepository)
    comprobantes: InMemoryComprobantesRepository = field(
        default_factory=InMemoryComprobantesRepository
    )
    items: InMemoryComprobanteItemsRepository = field(
        default_factory=InMemoryComprobanteItemsRepository
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
class FakeStorageProvider:
    """Doble del `StorageProvider`: guarda las subidas y firma URLs ficticias."""

    def __init__(self) -> None:
        self.upload: dict[str, str] | None = None
        self.firmas: list[tuple[str, int | None]] = []

    async def upload_file(
        self, file_bytes: bytes, filename: str, content_type: str, folder_path: str
    ) -> FileUploadResult:
        self.upload = {
            "nombre": filename,
            "content_type": content_type,
            "carpeta": folder_path,
        }
        return FileUploadResult(
            file_key=f"{folder_path}/{filename}",
            size_bytes=len(file_bytes),
            content_type=content_type,
            hash_sha256=hashlib.sha256(file_bytes).hexdigest(),
        )

    async def get_download_url(self, file_key: str, *, expires_in: int | None = None) -> str:
        self.firmas.append((file_key, expires_in))
        return f"https://firmada/{file_key}"


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
    contenido = xml or cargar_cdr_xml("cdr_aceptado.xml")
    info = CdrInfo.model_validate(
        {
            "codigo": codigo or "0",
            "descripcion": mensaje or "La Factura numero F001-00000001, ha sido aceptada",
            "notas": ["El comprobante fue aceptado con observaciones"],
            "archivo": "R-20123456789-01-F001-00000001.xml",
            # Igual que en producción, el ZIP original queda disponible.
            "zip_bytes": empaquetar_cdr_zip(contenido),
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
        metodo="getStatusAR",
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
        metodo="getStatusAR",
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
        metodo="getStatusAR",
        endpoint=ENDPOINT_BILL,
        mensaje="No se pudo contactar a SUNAT (timeout o error de red)",
        duracion_ms=20000,
        error_tipo="sunat_unavailable",
    )


# --------------------------------------------------------------------------- #
# SIRE (propuesta RVIE)
# --------------------------------------------------------------------------- #
def fila_propuesta(ruc: str, **campos: str) -> dict[str, str]:
    """Fila de la propuesta RVIE con valores válidos; `campos` pisa lo indicado."""
    base = {
        "Ruc": ruc,
        "Razon Social": "ACME SAC",
        "Periodo": "202609",
        "CAR SUNAT": "2051483162001FF010000001",
        "Fecha de emisión": "01/09/2026",
        "Fecha Vcto/Pago": "",
        "Tipo CP/Doc.": "01",
        "Serie del CDP": "FF01",
        "Nro CP o Doc. Nro Inicial (Rango)": "1",
        "Nro Final (Rango)": "",
        "Tipo Doc Identidad": "6",
        "Nro Doc Identidad": "20987654321",
        "Apellidos Nombres/ Razón Social": "CLIENTE EJEMPLO SAC",
        "BI Gravada": "100.00",
        "IGV / IPM": "18.00",
        "Total CP": "118.00",
        "Moneda": "PEN",
        "Tipo Cambio": "1.000",
        "Est. Comp": "1",
    }
    base.update(campos)
    return base


def construir_zip_propuesta(
    filas: list[dict[str, str]],
    *,
    nombre: str = "LE20123456789202610014040001EXP2.txt",
    cabecera: tuple[str, ...] | None = None,
) -> bytes:
    """ZIP con un TXT de la propuesta en el formato real (`|`, UTF-8 con BOM)."""
    import io
    import zipfile

    from app.sunat.sire_rvie import CABECERA

    columnas = cabecera or CABECERA
    lineas = ["|".join(columnas)]
    for fila in filas:
        lineas.append("|".join(fila.get(columna, "") for columna in columnas))
    texto = "\r\n".join(lineas) + "\r\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(nombre, texto.encode("utf-8-sig"))
    return buffer.getvalue()


@dataclass
class FakeSireClient:
    """Cliente SIRE simulado: el ticket recorre `estados` y luego entrega `zips`."""

    estados: list[str] = field(default_factory=lambda: ["06"])
    zips: list[bytes] = field(default_factory=list)
    ticket: str = "20260300000001"
    error_solicitud: Exception | None = None
    error_ticket: Exception | None = None
    credentials: Any = None
    solicitudes: list[str] = field(default_factory=list)
    consultas: int = 0
    descargas: int = 0

    def solicitar_propuesta(self, periodo: str) -> str:
        self.solicitudes.append(periodo)
        if self.error_solicitud is not None:
            raise self.error_solicitud
        return self.ticket

    def consultar_ticket(self, ticket: str, periodo: str):
        from app.sunat.sire import ArchivoTicket, EstadoTicket

        if self.error_ticket is not None:
            raise self.error_ticket
        codigo = self.estados[min(self.consultas, len(self.estados) - 1)]
        self.consultas += 1
        archivos = (
            tuple(ArchivoTicket(f"parte{i}.zip", "01") for i, _ in enumerate(self.zips))
            if codigo == "06"
            else ()
        )
        return EstadoTicket(
            numero=ticket,
            periodo=periodo,
            codigo_estado=codigo,
            descripcion="",
            proceso="10",
            archivos=archivos,
        )

    def descargar_archivo(self, estado: Any, archivo: Any) -> bytes:
        self.descargas += 1
        return self.zips[int(archivo.nombre.removeprefix("parte").removesuffix(".zip"))]


def fabricar_cliente_sire(cliente: FakeSireClient):
    """Fábrica compatible con `app.state.sire_client_factory`."""

    def factory(credentials: Any, settings: Any = None) -> FakeSireClient:
        cliente.credentials = credentials
        return cliente

    return factory
