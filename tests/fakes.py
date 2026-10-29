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
    CampoOrden,
    DireccionOrden,
    EstadoComprobante,
    OrigenComprobante,
    RespaldoFiltro,
    RolEmpresa,
    SunatAmbiente,
    TipoArchivo,
)
from app.core.errors import ConflictError, NotFoundError
from app.db.models import (
    Archivo,
    ArchivoRegistro,
    AutomatizacionLog,
    AutomatizacionLogRegistro,
    BizlinksConsulta,
    BizlinksConsultaRegistro,
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
from app.db.repositories.base import (
    FilaSap,
    FiltrosListado,
    PaginaComprobantes,
    ResumenComprobantes,
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

    async def get_access(self, ruc: str, user_id: UUID) -> tuple[Company, Membership | None] | None:
        empresa = await self.get_by_ruc(ruc)
        if empresa is None:
            return None
        return empresa, await self.get_membership(user_id, empresa.id)

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
    # Para los filtros por respaldo: se enlaza con el repo de archivos del mismo UoW.
    archivos_repo: InMemoryArchivosRepository | None = None

    async def get(self, comprobante_id: UUID) -> Comprobante | None:
        comprobante = self.comprobantes.get(comprobante_id)
        if comprobante is None or comprobante.company_id not in self.company_ids_visibles:
            return None
        return comprobante

    async def list_for_company(
        self,
        company_id: UUID,
        filtros: FiltrosListado | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
        contar: bool = False,
    ) -> PaginaComprobantes:
        filtros = filtros or FiltrosListado()
        visibles = self._filtrar(company_id, filtros)
        total = len(visibles)
        visibles = self._ordenar(visibles, filtros)
        pagina = visibles[offset : offset + limit]
        return PaginaComprobantes(
            items=pagina,
            total=total if contar else None,
            tipos={c.id: self._tipos(c) for c in pagina},
        )

    def _tipos(self, comprobante: Comprobante) -> set[TipoArchivo]:
        if self.archivos_repo is None:
            return set()
        return {
            a.tipo
            for a in self.archivos_repo.archivos
            if a.comprobante_id == comprobante.id and a.estado.value == "subido"
        }

    def _filtrar(self, company_id: UUID, filtros: FiltrosListado) -> list[Comprobante]:
        if company_id not in self.company_ids_visibles:
            return []
        f = filtros
        resultado = []
        for c in self.comprobantes.values():
            if c.company_id != company_id:
                continue
            if f.estado is not None and c.estado is not f.estado:
                continue
            if f.origen is not None and c.origen is not f.origen:
                continue
            if f.fuente is not None and c.fuente != f.fuente:
                continue
            fecha = c.fecha_emision or date.min
            if f.fecha_desde is not None and fecha < f.fecha_desde:
                continue
            if f.fecha_hasta is not None and fecha > f.fecha_hasta:
                continue
            if f.tipo_documento is not None and c.tipo_documento != f.tipo_documento:
                continue
            if f.serie is not None and c.serie != f.serie:
                continue
            if f.moneda is not None and c.moneda != f.moneda:
                continue
            # Sin vencimiento informado (p. ej. contado) rige la fecha de emisión.
            vence = c.fecha_vencimiento or c.fecha_emision
            if f.vencimiento_desde is not None and (vence is None or vence < f.vencimiento_desde):
                continue
            if f.vencimiento_hasta is not None and (vence is None or vence > f.vencimiento_hasta):
                continue
            if f.n_sap is not None and f.n_sap.lower() not in (c.n_sap or "").lower():
                continue
            if f.q is not None:
                # Equivale al `ilike %q%` de Postgres sobre los mismos campos.
                campos = [
                    f"{c.serie}-{c.numero}",
                    c.razon_social_receptor or "",
                    c.ruc_receptor or "",
                    c.orden_compra or "",
                ]
                if f.origen is OrigenComprobante.recibido:
                    campos += [c.razon_social_emisor or "", c.ruc_emisor, c.correo_asunto or ""]
                if not any(f.q.lower() in campo.lower() for campo in campos):
                    continue
            tipos = self._tipos(c)
            if f.respaldo is RespaldoFiltro.sin_xml and TipoArchivo.xml in tipos:
                continue
            if f.respaldo is RespaldoFiltro.sin_cdr and TipoArchivo.cdr in tipos:
                continue
            if f.respaldo is RespaldoFiltro.completo and not (
                TipoArchivo.xml in tipos and TipoArchivo.cdr in tipos
            ):
                continue
            xml, pdf = TipoArchivo.xml in tipos, TipoArchivo.pdf in tipos
            combinacion = {
                RespaldoFiltro.solo_xml: xml and not pdf,
                RespaldoFiltro.solo_pdf: pdf and not xml,
                RespaldoFiltro.xml_pdf: xml and pdf,
                RespaldoFiltro.ninguno: not xml and not pdf,
            }
            if f.respaldo in combinacion and not combinacion[f.respaldo]:
                continue
            resultado.append(c)
        return resultado

    @staticmethod
    def _ordenar(filas: list[Comprobante], f: FiltrosListado) -> list[Comprobante]:
        inverso = f.direccion is DireccionOrden.desc

        def importe(c: Comprobante) -> Decimal:
            monto = c.monto_total or Decimal(0)
            return -monto if c.tipo_documento == "07" else monto

        if f.orden is CampoOrden.numero:

            def clave(c: Comprobante):
                return (c.serie, c.correlativo)
        elif f.orden is CampoOrden.cliente:

            def clave(c: Comprobante):
                return ((c.razon_social_receptor or "").lower(), c.correlativo)
        elif f.orden is CampoOrden.importe:

            def clave(c: Comprobante):
                return (importe(c), c.correlativo)
        else:

            def clave(c: Comprobante):
                return (c.fecha_emision or date.min, c.correlativo)

        return sorted(filas, key=clave, reverse=inverso)

    async def resumen(
        self, company_id: UUID, filtros: FiltrosListado | None = None
    ) -> ResumenComprobantes:
        filas = self._filtrar(company_id, filtros or FiltrosListado())
        exigibles = [c for c in filas if c.estado is not EstadoComprobante.anulado]
        facturado: dict[str, Decimal] = {}
        for c in filas:
            if c.estado in (EstadoComprobante.anulado, EstadoComprobante.rechazado):
                continue
            monto = c.monto_total or Decimal(0)
            facturado[c.moneda] = facturado.get(c.moneda, Decimal(0)) + (
                -monto if c.tipo_documento == "07" else monto
            )
        return ResumenComprobantes(
            total=len(filas),
            por_estado={e: sum(1 for c in filas if c.estado is e) for e in EstadoComprobante},
            facturado=facturado,
            exigibles=len(exigibles),
            con_xml=sum(1 for c in exigibles if TipoArchivo.xml in self._tipos(c)),
            con_cdr=sum(1 for c in exigibles if TipoArchivo.cdr in self._tipos(c)),
            con_pdf=sum(1 for c in exigibles if TipoArchivo.pdf in self._tipos(c)),
            correos_enviados=sum(1 for c in filas if (c.correo_asunto or "").strip()),
        )

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
                and c.ruc_emisor == registro.ruc_emisor
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

    async def find_recibidas(
        self, company_id: UUID, *, ruc_emisor: str, serie: str, correlativo: int
    ) -> list[Comprobante]:
        return [
            c
            for c in self.comprobantes.values()
            if c.company_id == company_id
            and c.origen is OrigenComprobante.recibido
            and c.ruc_emisor == ruc_emisor
            and c.serie == serie
            and c.correlativo == correlativo
        ]

    async def find_emitido(
        self, company_id: UUID, *, tipo_documento: str, serie: str, correlativo: int
    ) -> Comprobante | None:
        candidatos = [
            c
            for c in self.comprobantes.values()
            if c.company_id == company_id
            and c.origen is OrigenComprobante.emitido
            and c.tipo_documento == tipo_documento
            and c.serie == serie
            and c.correlativo == correlativo
        ]
        candidatos.sort(key=lambda c: c.sunat_ambiente is SunatAmbiente.prod, reverse=True)
        return candidatos[0] if candidatos else None

    def _buscar(
        self,
        company_id: UUID,
        *,
        origen: OrigenComprobante,
        tipo_documento: str,
        serie: str,
        correlativo: int,
        sunat_ambiente: SunatAmbiente,
        ruc_emisor: str | None = None,
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
                and (ruc_emisor is None or c.ruc_emisor == ruc_emisor)
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
        ruc_emisor: str | None = None,
    ) -> Comprobante | None:
        return self._buscar(
            company_id,
            origen=origen,
            tipo_documento=tipo_documento,
            serie=serie,
            correlativo=correlativo,
            sunat_ambiente=sunat_ambiente,
            ruc_emisor=ruc_emisor,
        )

    async def create_if_absent(self, registro: ComprobanteRegistro) -> Comprobante | None:
        existente = self._buscar(
            registro.company_id,
            origen=registro.origen,
            tipo_documento=registro.tipo_documento,
            serie=registro.serie,
            correlativo=registro.correlativo,
            sunat_ambiente=registro.sunat_ambiente,
            ruc_emisor=registro.ruc_emisor,
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

    async def registrar_correo(
        self,
        company_id: UUID,
        *,
        ruc_emisor: str,
        serie: str,
        correlativo: int,
        asunto: str,
        remitente: str | None,
        recibido_at: datetime,
    ) -> int:
        cuenta = 0
        for id_, c in list(self.comprobantes.items()):
            coincide = (
                c.company_id == company_id
                and c.origen is OrigenComprobante.recibido
                and (c.ruc_emisor, c.serie, c.correlativo) == (ruc_emisor, serie, correlativo)
            )
            if coincide and (c.correo_recibido_at is None or recibido_at < c.correo_recibido_at):
                self.comprobantes[id_] = c.model_copy(
                    update={
                        "correo_asunto": asunto,
                        "correo_remitente": remitente,
                        "correo_recibido_at": recibido_at,
                    }
                )
                cuenta += 1
        return cuenta

    async def actualizar_pago(
        self, comprobante_id: UUID, *, fecha_vencimiento: date | None, forma_pago: str | None
    ) -> Comprobante:
        comprobante = self.comprobantes.get(comprobante_id)
        if comprobante is None:
            raise NotFoundError("El comprobante no existe")
        actualizado = comprobante.model_copy(
            update={
                # El SQL real usa coalesce: los valores None conservan lo anterior.
                "fecha_vencimiento": fecha_vencimiento or comprobante.fecha_vencimiento,
                "forma_pago": forma_pago or comprobante.forma_pago,
            }
        )
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
        n_sap: str | None = None,
    ) -> Comprobante:
        comprobante = self.comprobantes.get(comprobante_id)
        if comprobante is None:
            raise NotFoundError("El comprobante no existe")
        actualizado = comprobante.model_copy(update={"n_sap": n_sap})
        self.comprobantes[comprobante_id] = actualizado
        return actualizado

    async def asignar_n_sap_lote(self, company_id: UUID, filas: list[FilaSap]) -> set[int]:
        encontradas: set[int] = set()
        for posicion, fila in enumerate(filas):
            for id_, c in list(self.comprobantes.items()):
                if c.company_id != company_id or c.serie != fila.serie:
                    continue
                if c.correlativo != fila.correlativo:
                    continue
                if fila.ruc is not None and fila.ruc not in (c.ruc_receptor, c.ruc_emisor):
                    continue
                self.comprobantes[id_] = c.model_copy(update={"n_sap": fila.n_sap})
                encontradas.add(posicion)
        return encontradas


@dataclass
class InMemoryComprobanteItemsRepository:
    items: list[ComprobanteItem] = field(default_factory=list)

    async def create_many(self, registros: list[ComprobanteItemRegistro]) -> list[ComprobanteItem]:
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
class InMemoryAutomatizacionLogsRepository:
    """Corridas en memoria (la fila más reciente primero, como el SQL)."""

    logs: list[AutomatizacionLog] = field(default_factory=list)
    company_ids_visibles: set[UUID] = field(default_factory=set)

    async def registrar(self, registro: AutomatizacionLogRegistro) -> AutomatizacionLog:
        log = AutomatizacionLog(
            id=uuid4(), created_at=datetime.now(tz=UTC), **registro.model_dump()
        )
        self.logs.append(log)
        return log

    async def listar(
        self,
        company_id: UUID,
        *,
        resultado: str | None,
        limit: int,
        offset: int,
        origen: str | None = None,
    ) -> tuple[list[AutomatizacionLog], int]:
        filas = [
            log
            for log in self.logs
            if log.company_id == company_id
            and (resultado is None or log.resultado == resultado)
            and (origen is None or (origen == "portal") == ("portal" in log.tareas))
        ]
        filas.sort(key=lambda log: log.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        return filas[offset : offset + limit], len(filas)


@dataclass
class InMemoryBizlinksConsultasRepository:
    """Bitácora en memoria con la misma semántica que las consultas SQL."""

    consultas: list[BizlinksConsulta] = field(default_factory=list)

    async def registrar(self, registro: BizlinksConsultaRegistro) -> BizlinksConsulta:
        consulta = BizlinksConsulta(
            id=uuid4(),
            created_at=datetime.now(tz=UTC),
            **registro.model_dump(),
        )
        self.consultas.append(consulta)
        return consulta

    async def ultimo_bloqueo_desde(
        self, company_id: UUID, canal: str, desde: datetime
    ) -> datetime | None:
        fechas = [
            c.created_at
            for c in self.consultas
            if c.company_id == company_id
            and c.canal == canal
            and c.resultado == "bloqueado"
            and c.created_at is not None
            and c.created_at >= desde
        ]
        return max(fechas) if fechas else None

    async def comprobantes_consultados_desde(
        self, company_id: UUID, canal: str, desde: datetime
    ) -> int:
        ids = {
            c.comprobante_id
            for c in self.consultas
            if c.company_id == company_id
            and c.canal == canal
            and c.comprobante_id is not None
            and c.created_at is not None
            and c.created_at >= desde
        }
        return len(ids)

    async def sin_exito_desde(
        self, company_id: UUID, comprobante_ids: list[UUID], canal: str, desde: datetime
    ) -> set[UUID]:
        return {
            c.comprobante_id
            for c in self.consultas
            if c.company_id == company_id
            and c.comprobante_id in comprobante_ids
            and c.canal == canal
            and c.resultado == "sin_resultado"
            and c.created_at is not None
            and c.created_at >= desde
        }


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

    async def tipos_por_comprobante(
        self, comprobante_ids: list[UUID]
    ) -> dict[UUID, set[TipoArchivo]]:
        tipos: dict[UUID, set[TipoArchivo]] = {}
        for a in self.archivos:
            if (
                a.comprobante_id in comprobante_ids
                and a.company_id in self.company_ids_visibles
                and a.estado.value == "subido"
            ):
                tipos.setdefault(a.comprobante_id, set()).add(a.tipo)
        return tipos

    async def list_evidencias(self, company_id: UUID, *, tipo: TipoArchivo) -> list[Archivo]:
        visibles = [
            a
            for a in self.archivos
            if a.company_id == company_id
            and a.tipo is tipo
            and a.comprobante_id is None
            and a.company_id in self.company_ids_visibles
        ]
        return sorted(visibles, key=lambda a: a.created_at or a.subido_at, reverse=True)

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
    bizlinks_consultas: InMemoryBizlinksConsultasRepository = field(
        default_factory=InMemoryBizlinksConsultasRepository
    )
    automatizacion_logs: InMemoryAutomatizacionLogsRepository = field(
        default_factory=InMemoryAutomatizacionLogsRepository
    )
    claims: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.comprobantes.archivos_repo = self.archivos


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
        self.subidas: list[dict[str, str]] = []
        self.objetos: dict[str, bytes] = {}

    async def get_object_bytes(self, file_key: str) -> bytes:
        return self.objetos[file_key]

    async def upload_file(
        self, file_bytes: bytes, filename: str, content_type: str, folder_path: str
    ) -> FileUploadResult:
        self.subidas.append({"nombre": filename, "carpeta": folder_path})
        self.objetos[f"{folder_path}/{filename}"] = file_bytes
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
    libros: list[str] = field(default_factory=list)
    consultas: int = 0
    descargas: int = 0

    def solicitar_propuesta(self, periodo: str, libro: str = "rvie") -> str:
        self.solicitudes.append(periodo)
        self.libros.append(libro)
        if self.error_solicitud is not None:
            raise self.error_solicitud
        return self.ticket

    def consultar_ticket(self, ticket: str, periodo: str, libro: str = "rvie"):
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

    def descargar_archivo(self, estado: Any, archivo: Any, libro: str = "rvie") -> bytes:
        self.descargas += 1
        return self.zips[int(archivo.nombre.removeprefix("parte").removesuffix(".zip"))]


def fabricar_cliente_sire(cliente: FakeSireClient):
    """Fábrica compatible con `app.state.sire_client_factory`."""

    def factory(credentials: Any, settings: Any = None) -> FakeSireClient:
        cliente.credentials = credentials
        return cliente

    return factory
