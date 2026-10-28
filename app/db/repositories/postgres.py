"""Implementación de los repositorios sobre asyncpg.

Todas las consultas dependen de las políticas RLS: la sesión ya viene con los
claims del usuario y el rol `authenticated`.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import asyncpg

from app.core.enums import (
    CampoOrden,
    EstadoComprobante,
    OrigenComprobante,
    RespaldoFiltro,
    SunatAmbiente,
    TipoArchivo,
)
from app.core.errors import ConflictError
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

_COLUMNAS_COMPROBANTE = """
    id, company_id, origen, ruc_emisor, ruc_receptor, receptor_doc, razon_social_emisor,
    razon_social_receptor, tipo_documento, serie, correlativo, numero, nombre_archivo,
    fecha_emision, fecha_vencimiento, forma_pago, moneda, monto_total, tipo_cambio, monto_pen,
    emisor_tipo_doc, receptor_tipo_doc, estado, sunat_ambiente,
    n_sap, orden_compra, correo_asunto, correo_remitente, correo_recibido_at,
    cdr_codigo, cdr_descripcion, cdr_notas, cdr_fecha, ultimo_envio_at, anulado_at,
    motivo_anulacion, created_at, updated_at, created_by, fuente
"""

_COLUMNAS_COMPROBANTE_C = ", ".join(
    f"c.{columna.strip()}" for columna in _COLUMNAS_COMPROBANTE.split(",") if columna.strip()
)


def _donde(company_id: UUID, filtros: FiltrosListado) -> tuple[str, list[object]]:
    """Cláusula `where` (alias `c`) y sus parámetros; compartida por página y resumen."""
    parametros: list[object] = [company_id]
    condiciones = ["c.company_id = $1"]

    def agregar(plantilla: str, valor: object) -> None:
        parametros.append(valor)
        condiciones.append(plantilla.format(n=len(parametros)))

    if filtros.estado is not None:
        agregar("c.estado = ${n}", filtros.estado.value)
    if filtros.origen is not None:
        agregar("c.origen = ${n}", filtros.origen.value)
    if filtros.fuente is not None:
        agregar("c.fuente = ${n}", filtros.fuente)
    if filtros.fecha_desde is not None:
        agregar("c.fecha_emision >= ${n}", filtros.fecha_desde)
    if filtros.fecha_hasta is not None:
        agregar("c.fecha_emision <= ${n}", filtros.fecha_hasta)
    if filtros.tipo_documento is not None:
        agregar("c.tipo_documento = ${n}", filtros.tipo_documento)
    if filtros.serie is not None:
        agregar("c.serie = ${n}", filtros.serie)
    if filtros.moneda is not None:
        agregar("c.moneda = ${n}", filtros.moneda)
    if filtros.vencimiento_desde is not None:
        agregar("coalesce(c.fecha_vencimiento, c.fecha_emision) >= ${n}", filtros.vencimiento_desde)
    if filtros.vencimiento_hasta is not None:
        agregar("coalesce(c.fecha_vencimiento, c.fecha_emision) <= ${n}", filtros.vencimiento_hasta)
    if filtros.n_sap is not None:
        agregar("c.n_sap ilike ${n} escape '\\'", _patron_like(filtros.n_sap))
    if filtros.q is not None:
        parametros.append(_patron_like(filtros.q))
        n = len(parametros)
        # N° de documento, cliente, RUC y orden de compra. En compras (`origen=recibido`) la
        # contraparte es el proveedor, así que solo entonces se busca también por emisor.
        brazos = [
            f"(c.serie || '-' || c.numero) ilike ${n} escape '\\'",
            f"c.razon_social_receptor ilike ${n} escape '\\'",
            f"c.ruc_receptor ilike ${n} escape '\\'",
            f"c.orden_compra ilike ${n} escape '\\'",
        ]
        if filtros.origen is OrigenComprobante.recibido:
            brazos += [
                f"c.razon_social_emisor ilike ${n} escape '\\'",
                f"c.ruc_emisor ilike ${n} escape '\\'",
                f"c.correo_asunto ilike ${n} escape '\\'",
            ]
        condiciones.append("(" + " or ".join(brazos) + ")")
    # Cobertura de respaldo: columnas que mantiene el trigger de `archivos` (migración 0008).
    if filtros.respaldo is RespaldoFiltro.sin_xml:
        condiciones.append("not c.tiene_xml")
    elif filtros.respaldo is RespaldoFiltro.sin_cdr:
        condiciones.append("not c.tiene_cdr")
    elif filtros.respaldo is RespaldoFiltro.completo:
        condiciones.append("c.tiene_xml and c.tiene_cdr")
    return " and ".join(condiciones), parametros


def _orden(filtros: FiltrosListado) -> str:
    """`order by` desde una lista blanca (nunca texto del usuario).

    El desempate por `id` va en el mismo sentido y los nulos se ordenan como Postgres los
    pondría al recorrer el índice al revés: así `fecha` y `numero` salen directo de los
    índices de la migración 0008, sin ordenar todo el conjunto.
    """
    sentido = filtros.direccion.value
    nulos = "last" if sentido == "desc" else "first"
    if filtros.orden is CampoOrden.numero:
        principal = f"c.serie {sentido}, c.correlativo {sentido}"
    elif filtros.orden is CampoOrden.cliente:
        # Los comprobantes sin nombre (boletas a consumidor final) van siempre al final.
        principal = (
            "nullif(lower(case when c.origen = 'recibido' then coalesce(c.razon_social_emisor, '') "
            f"else coalesce(c.razon_social_receptor, '') end), '') {sentido} nulls last, "
            "c.fecha_emision desc nulls last"
        )
    elif filtros.orden is CampoOrden.importe:
        # Las notas de crédito restan, así que ordenan como negativas.
        principal = (
            "(case when c.tipo_documento = '07' then -c.monto_total else c.monto_total end) "
            f"{sentido} nulls {nulos}, c.fecha_emision desc nulls last"
        )
    else:
        principal = f"c.fecha_emision {sentido} nulls {nulos}, c.correlativo {sentido}"
    return f"{principal}, c.id {sentido}"


_COLUMNAS_ITEM = """
    id, comprobante_id, item_nro, codigo, descripcion, unidad_medida, cantidad,
    valor_unitario, precio_unitario, descuento, igv, isc, tip_afectacion_igv, total,
    created_at, updated_at
"""


def _row(record: asyncpg.Record | None) -> dict[str, Any] | None:
    return dict(record) if record is not None else None


def _patron_like(texto: str) -> str:
    """Patrón `contains` para `ilike`, con los comodines del usuario escapados."""
    escapado = texto.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escapado}%"


class PostgresCompaniesRepository:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    async def list_for_user(self, user_id: UUID) -> list[CompanyMember]:
        records = await self._connection.fetch(
            """
            select c.id, c.ruc, c.razon_social, c.nombre_comercial, c.direccion, c.ubigeo,
                   c.activo, c.sunat_ambiente, c.certificado_alias, c.certificado_expira_at,
                   c.created_at, c.updated_at, uc.rol
            from public.user_companies uc
            join public.companies c on c.id = uc.company_id
            where uc.user_id = $1 and uc.activo and c.activo
            order by c.razon_social
            """,
            user_id,
        )
        return [
            CompanyMember(company=Company.model_validate(dict(record)), rol=record["rol"])
            for record in records
        ]

    async def get_by_ruc(self, ruc: str) -> Company | None:
        record = await self._connection.fetchrow(
            "select * from public.companies where ruc = $1", ruc
        )
        data = _row(record)
        return Company.model_validate(data) if data else None

    async def get_by_id(self, company_id: UUID) -> Company | None:
        record = await self._connection.fetchrow(
            "select * from public.companies where id = $1", company_id
        )
        data = _row(record)
        return Company.model_validate(data) if data else None

    async def get_access(
        self, ruc: str, user_id: UUID
    ) -> tuple[Company, Membership | None] | None:
        # Un solo viaje a la base en vez de dos: es lo que más pesa con la base remota.
        record = await self._connection.fetchrow(
            """
            select c.*, uc.rol as _rol, uc.activo as _activo
            from public.companies c
            left join public.user_companies uc
                   on uc.company_id = c.id and uc.user_id = $2
            where c.ruc = $1
            """,
            ruc,
            user_id,
        )
        if record is None:
            return None
        data = dict(record)
        rol, activo = data.pop("_rol"), data.pop("_activo")
        company = Company.model_validate(data)
        if rol is None:
            return company, None
        membership = Membership(
            user_id=user_id, company_id=company.id, rol=rol, activo=bool(activo)
        )
        return company, membership

    async def get_membership(self, user_id: UUID, company_id: UUID) -> Membership | None:
        record = await self._connection.fetchrow(
            """
            select user_id, company_id, rol, activo
            from public.user_companies
            where user_id = $1 and company_id = $2
            """,
            user_id,
            company_id,
        )
        data = _row(record)
        return Membership.model_validate(data) if data else None


class PostgresComprobantesRepository:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    async def get(self, comprobante_id: UUID) -> Comprobante | None:
        record = await self._connection.fetchrow(
            f"select {_COLUMNAS_COMPROBANTE} from public.comprobantes where id = $1",
            comprobante_id,
        )
        data = _row(record)
        return Comprobante.model_validate(data) if data else None

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
        donde, parametros = _donde(company_id, filtros)
        parametros.extend([limit, offset])
        limite_idx, offset_idx = len(parametros) - 1, len(parametros)
        # `count(*) over()` evita una segunda consulta (cada ida y vuelta a la base cuesta).
        total_sql = ", count(*) over() as _total" if contar else ""
        records = await self._connection.fetch(
            f"""
            select {_COLUMNAS_COMPROBANTE_C}, c.tiene_xml, c.tiene_cdr, c.tiene_pdf{total_sql}
            from public.comprobantes c
            where {donde}
            order by {_orden(filtros)}
            limit ${limite_idx} offset ${offset_idx}
            """,
            *parametros,
        )
        total: int | None = None
        if contar:
            if records:
                total = records[0]["_total"]
            elif offset == 0:
                total = 0
            else:  # página más allá del final: se cuenta aparte para no mentir con 0
                total = await self._contar(company_id, filtros)
        items: list[Comprobante] = []
        tipos: dict[UUID, set[TipoArchivo]] = {}
        for record in records:
            fila = dict(record)
            fila.pop("_total", None)
            subidos = {
                tipo
                for tipo, columna in (
                    (TipoArchivo.xml, "tiene_xml"),
                    (TipoArchivo.cdr, "tiene_cdr"),
                    (TipoArchivo.pdf, "tiene_pdf"),
                )
                if fila.pop(columna)
            }
            comprobante = Comprobante.model_validate(fila)
            items.append(comprobante)
            tipos[comprobante.id] = subidos
        return PaginaComprobantes(items=items, total=total, tipos=tipos)

    async def _contar(self, company_id: UUID, filtros: FiltrosListado) -> int:
        donde, parametros = _donde(company_id, filtros)
        total = await self._connection.fetchval(
            f"select count(*) from public.comprobantes c where {donde}", *parametros
        )
        return int(total)

    async def resumen(
        self, company_id: UUID, filtros: FiltrosListado | None = None
    ) -> ResumenComprobantes:
        filtros = filtros or FiltrosListado()
        donde, parametros = _donde(company_id, filtros)
        fila = await self._connection.fetchrow(
            f"""
            select
                count(*) as total,
                count(*) filter (where c.estado = 'pendiente') as pendiente,
                count(*) filter (where c.estado = 'aprobado') as aprobado,
                count(*) filter (where c.estado = 'rechazado') as rechazado,
                count(*) filter (where c.estado = 'anulado') as anulado,
                count(*) filter (where c.estado <> 'anulado') as exigibles,
                count(*) filter (where c.estado <> 'anulado' and c.tiene_xml) as con_xml,
                count(*) filter (where c.estado <> 'anulado' and c.tiene_cdr) as con_cdr,
                count(*) filter (where c.estado <> 'anulado' and c.tiene_pdf) as con_pdf
            from public.comprobantes c
            where {donde}
            """,
            *parametros,
        )
        netos = await self._connection.fetch(
            f"""
            select c.moneda,
                   sum(case when c.tipo_documento = '07' then -c.monto_total
                            else c.monto_total end) as neto
            from public.comprobantes c
            where {donde} and c.estado not in ('anulado', 'rechazado')
            group by c.moneda
            """,
            *parametros,
        )
        assert fila is not None
        return ResumenComprobantes(
            total=fila["total"],
            por_estado={
                EstadoComprobante(nombre): fila[nombre]
                for nombre in ("pendiente", "aprobado", "rechazado", "anulado")
            },
            facturado={r["moneda"]: r["neto"] or Decimal("0") for r in netos},
            exigibles=fila["exigibles"],
            con_xml=fila["con_xml"],
            con_cdr=fila["con_cdr"],
            con_pdf=fila["con_pdf"],
        )

    async def create(self, registro: ComprobanteRegistro) -> Comprobante:
        creado = await self._insertar(registro, ignorar_duplicado=False)
        assert creado is not None
        return creado

    async def create_if_absent(self, registro: ComprobanteRegistro) -> Comprobante | None:
        """Inserta o devuelve `None` si ya existía (no aborta la transacción)."""
        return await self._insertar(registro, ignorar_duplicado=True)

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
        record = await self._connection.fetchrow(
            f"""
            select {_COLUMNAS_COMPROBANTE} from public.comprobantes
            where company_id = $1 and origen = $2 and tipo_documento = $3
              and serie = $4 and correlativo = $5 and sunat_ambiente = $6
              and ($7::text is null or ruc_emisor = $7)
            """,
            company_id,
            origen.value,
            tipo_documento,
            serie,
            correlativo,
            sunat_ambiente.value,
            ruc_emisor,
        )
        data = _row(record)
        return Comprobante.model_validate(data) if data else None

    async def find_recibidas(
        self, company_id: UUID, *, ruc_emisor: str, serie: str, correlativo: int
    ) -> list[Comprobante]:
        registros = await self._connection.fetch(
            f"""
            select {_COLUMNAS_COMPROBANTE} from public.comprobantes
            where company_id = $1 and origen = 'recibido' and ruc_emisor = $2
              and serie = $3 and correlativo = $4
            """,
            company_id,
            ruc_emisor,
            serie,
            correlativo,
        )
        return [Comprobante.model_validate(_row(r)) for r in registros]

    async def find_emitido(
        self, company_id: UUID, *, tipo_documento: str, serie: str, correlativo: int
    ) -> Comprobante | None:
        record = await self._connection.fetchrow(
            f"""
            select {_COLUMNAS_COMPROBANTE} from public.comprobantes
            where company_id = $1 and origen = 'emitido' and tipo_documento = $2
              and serie = $3 and correlativo = $4
            order by (sunat_ambiente = 'prod') desc
            limit 1
            """,
            company_id,
            tipo_documento,
            serie,
            correlativo,
        )
        data = _row(record)
        return Comprobante.model_validate(data) if data else None

    async def completar_desde_xml(
        self, comprobante_id: UUID, registro: ComprobanteRegistro
    ) -> Comprobante:
        """Un XML importado reemplaza la cabecera de un registro que vino de SIRE."""
        record = await self._connection.fetchrow(
            f"""
            update public.comprobantes
               set ruc_receptor = $2, receptor_doc = $3, razon_social_emisor = $4,
                   razon_social_receptor = $5, nombre_archivo = $6, fecha_emision = $7,
                   fecha_vencimiento = $8, forma_pago = $9, moneda = $10, monto_total = $11,
                   fuente = 'xml', updated_at = now()
             where id = $1
            returning {_COLUMNAS_COMPROBANTE}
            """,
            comprobante_id,
            registro.ruc_receptor,
            registro.receptor_doc,
            registro.razon_social_emisor,
            registro.razon_social_receptor,
            registro.nombre_archivo,
            registro.fecha_emision,
            registro.fecha_vencimiento,
            registro.forma_pago,
            registro.moneda,
            registro.monto_total,
        )
        data = _row(record)
        if data is None:
            from app.core.errors import NotFoundError

            raise NotFoundError("El comprobante no existe o no pertenece a sus empresas")
        return Comprobante.model_validate(data)

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
        """Anota de qué correo llegó una compra. Si ya tenía otro, gana el que llegó primero."""
        resultado = await self._connection.execute(
            """
            update public.comprobantes
               set correo_asunto = $5, correo_remitente = $6, correo_recibido_at = $7
             where company_id = $1 and origen = 'recibido'
               and ruc_emisor = $2 and serie = $3 and correlativo = $4
               and (correo_recibido_at is null or $7 < correo_recibido_at)
            """,
            company_id,
            ruc_emisor,
            serie,
            correlativo,
            asunto,
            remitente,
            recibido_at,
        )
        return int(resultado.rsplit(" ", 1)[-1])

    async def actualizar_pago(
        self, comprobante_id: UUID, *, fecha_vencimiento: date | None, forma_pago: str | None
    ) -> Comprobante:
        """El XML de respaldo completa el vencimiento y la forma de pago que SIRE no trae.

        Un valor `None` conserva el que ya tenía el comprobante (coalesce).
        """
        record = await self._connection.fetchrow(
            f"""
            update public.comprobantes
               set fecha_vencimiento = coalesce($2, fecha_vencimiento),
                   forma_pago = coalesce($3, forma_pago),
                   updated_at = now()
             where id = $1
            returning {_COLUMNAS_COMPROBANTE}
            """,
            comprobante_id,
            fecha_vencimiento,
            forma_pago,
        )
        data = _row(record)
        if data is None:
            from app.core.errors import NotFoundError

            raise NotFoundError("El comprobante no existe o no pertenece a sus empresas")
        return Comprobante.model_validate(data)

    async def _insertar(
        self, registro: ComprobanteRegistro, *, ignorar_duplicado: bool
    ) -> Comprobante | None:
        # `on conflict do nothing` evita el error de unicidad, que dejaría
        # abortada la transacción de la petición en las cargas masivas.
        al_conflicto = (
            "on conflict on constraint comprobantes_unicos_por_ambiente do nothing"
            if ignorar_duplicado
            else ""
        )
        try:
            record = await self._connection.fetchrow(
                f"""
                insert into public.comprobantes (
                    company_id, origen, ruc_emisor, ruc_receptor, receptor_doc,
                    razon_social_emisor, razon_social_receptor, tipo_documento, serie,
                    correlativo, numero, nombre_archivo, fecha_emision, fecha_vencimiento,
                    forma_pago, moneda, monto_total, estado, sunat_ambiente, created_by, fuente,
                    tipo_cambio, monto_pen, emisor_tipo_doc, receptor_tipo_doc
                )
                values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15,
                        $16, $17, $18, $19, $20, $21, $22, $23, $24, $25)
                {al_conflicto}
                returning {_COLUMNAS_COMPROBANTE}
                """,
                registro.company_id,
                registro.origen.value,
                registro.ruc_emisor,
                registro.ruc_receptor,
                registro.receptor_doc,
                registro.razon_social_emisor,
                registro.razon_social_receptor,
                registro.tipo_documento,
                registro.serie,
                registro.correlativo,
                registro.numero,
                registro.nombre_archivo,
                registro.fecha_emision,
                registro.fecha_vencimiento,
                registro.forma_pago,
                registro.moneda,
                registro.monto_total,
                registro.estado.value,
                registro.sunat_ambiente.value,
                registro.created_by,
                registro.fuente,
                registro.tipo_cambio,
                registro.monto_pen,
                registro.emisor_tipo_doc,
                registro.receptor_tipo_doc,
            )
        except asyncpg.UniqueViolationError as exc:
            # Restricción comprobantes_unicos_por_ambiente.
            raise ConflictError(
                "Ese comprobante ya está registrado para la empresa en el mismo ambiente"
            ) from exc
        data = _row(record)
        return Comprobante.model_validate(data) if data else None

    async def update_sunat_state(
        self,
        comprobante_id: UUID,
        *,
        estado: EstadoComprobante,
        cdr_codigo: str | None,
        cdr_descripcion: str | None,
        cdr_notas: list[str],
    ) -> Comprobante:
        record = await self._connection.fetchrow(
            f"""
            update public.comprobantes
               set estado =$2::public.estado_comprobante,
                   cdr_codigo = coalesce($3, cdr_codigo),
                   cdr_descripcion = coalesce($4, cdr_descripcion),
                   cdr_notas = case
                       when $5::jsonb = '[]'::jsonb then cdr_notas
                       else $5::jsonb
                   end,
                   cdr_fecha = case when $3 is null then cdr_fecha else now() end,
                   anulado_at = case
                       when $2::public.estado_comprobante = 'anulado'
                           then coalesce(anulado_at, now())
                       else anulado_at
                   end,
                   ultimo_envio_at = now(),
                   updated_at = now()
             where id = $1
            returning {_COLUMNAS_COMPROBANTE}
            """,
            comprobante_id,
            estado.value,
            cdr_codigo,
            cdr_descripcion,
            cdr_notas,
        )
        data = _row(record)
        if data is None:
            from app.core.errors import NotFoundError

            raise NotFoundError("El comprobante no existe o no pertenece a sus empresas")
        return Comprobante.model_validate(data)

    async def update_sap(
        self,
        comprobante_id: UUID,
        *,
        n_sap: str | None = None,
    ) -> Comprobante:
        record = await self._connection.fetchrow(
            f"""
            update public.comprobantes
               set n_sap = $2, updated_at = now()
             where id = $1
            returning {_COLUMNAS_COMPROBANTE}
            """,
            comprobante_id,
            n_sap,
        )
        data = _row(record)
        if data is None:
            from app.core.errors import NotFoundError

            raise NotFoundError("El comprobante no existe o no pertenece a sus empresas")
        return Comprobante.model_validate(data)

    async def asignar_n_sap_lote(self, company_id: UUID, filas: list[FilaSap]) -> set[int]:
        if not filas:
            return set()
        # Una sola sentencia para todo el Excel. `pos` devuelve qué filas encontraron
        # comprobante; sin RUC se empareja solo por serie + correlativo.
        encontradas = await self._connection.fetch(
            """
            with entrada as (
                select * from unnest($2::int[], $3::text[], $4::int[], $5::text[], $6::text[])
                    as e(pos, serie, correlativo, n_sap, ruc)
            )
            update public.comprobantes c
               set n_sap = e.n_sap, updated_at = now()
              from entrada e
             where c.company_id = $1
               and c.serie = e.serie
               and c.correlativo = e.correlativo
               and (e.ruc is null or e.ruc in (c.ruc_receptor, c.ruc_emisor))
            returning e.pos
            """,
            company_id,
            list(range(len(filas))),
            [f.serie for f in filas],
            [f.correlativo for f in filas],
            [f.n_sap for f in filas],
            [f.ruc for f in filas],
        )
        return {fila["pos"] for fila in encontradas}


class PostgresComprobanteItemsRepository:
    """Líneas del detalle (`comprobante_items`), bajo las mismas políticas RLS."""

    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    async def create_many(
        self, registros: list[ComprobanteItemRegistro]
    ) -> list[ComprobanteItem]:
        if not registros:
            return []
        filas = [
            (
                registro.comprobante_id,
                registro.item_nro,
                registro.codigo,
                registro.descripcion,
                registro.unidad_medida,
                registro.cantidad,
                registro.valor_unitario,
                registro.precio_unitario,
                registro.descuento,
                registro.igv,
                registro.isc,
                registro.tip_afectacion_igv,
                registro.total,
            )
            for registro in registros
        ]
        # `executemany` no soporta RETURNING: se releen al final, en la misma
        # transacción de la petición.
        await self._connection.executemany(
            """
            insert into public.comprobante_items (
                comprobante_id, item_nro, codigo, descripcion, unidad_medida, cantidad,
                valor_unitario, precio_unitario, descuento, igv, isc,
                tip_afectacion_igv, total
            )
            values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            """,
            filas,
        )
        return await self.list_for_comprobante(registros[0].comprobante_id)

    async def list_for_comprobante(self, comprobante_id: UUID) -> list[ComprobanteItem]:
        records = await self._connection.fetch(
            f"""
            select {_COLUMNAS_ITEM}
            from public.comprobante_items
            where comprobante_id = $1
            order by item_nro
            """,
            comprobante_id,
        )
        return [ComprobanteItem.model_validate(_row(record)) for record in records]


class PostgresSunatEnviosRepository:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    async def record(self, registro: SunatEnvioRegistro) -> SunatEnvio:
        record = await self._connection.fetchrow(
            """
            insert into public.sunat_envios (
                company_id, comprobante_id, tipo_operacion, sunat_ambiente, endpoint, metodo,
                request_xml, response_xml, http_status, codigo_respuesta, mensaje_respuesta,
                notas, exito, error_tipo, duracion_ms
            )
            values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
            returning id, company_id, comprobante_id, tipo_operacion, sunat_ambiente, endpoint,
                      metodo, request_xml, response_xml, http_status, codigo_respuesta,
                      mensaje_respuesta, notas, exito, error_tipo, duracion_ms, created_at
            """,
            registro.company_id,
            registro.comprobante_id,
            registro.tipo_operacion.value,
            registro.sunat_ambiente.value,
            registro.endpoint,
            registro.metodo,
            registro.request_xml,
            registro.response_xml,
            registro.http_status,
            registro.codigo_respuesta,
            registro.mensaje_respuesta,
            registro.notas,
            registro.exito,
            registro.error_tipo,
            registro.duracion_ms,
        )
        data = _row(record)
        assert data is not None  # un insert con returning siempre devuelve fila
        return SunatEnvio.model_validate(data)

    async def list_for_comprobante(
        self, comprobante_id: UUID, *, limit: int = 20
    ) -> list[SunatEnvio]:
        records = await self._connection.fetch(
            """
            select id, company_id, comprobante_id, tipo_operacion, sunat_ambiente, endpoint,
                   metodo, request_xml, response_xml, http_status, codigo_respuesta,
                   mensaje_respuesta, notas, exito, error_tipo, duracion_ms, created_at
            from public.sunat_envios
            where comprobante_id = $1
            order by created_at desc
            limit $2
            """,
            comprobante_id,
            limit,
        )
        return [SunatEnvio.model_validate(_row(record)) for record in records]


class PostgresArchivosRepository:
    """Referencias de archivos en Cloudflare R2 (bucket privado)."""

    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    async def get(self, archivo_id: UUID) -> Archivo | None:
        record = await self._connection.fetchrow(
            "select * from public.archivos where id = $1", archivo_id
        )
        data = _row(record)
        return Archivo.model_validate(data) if data else None

    async def list_for_comprobante(self, comprobante_id: UUID) -> list[Archivo]:
        records = await self._connection.fetch(
            "select * from public.archivos where comprobante_id = $1 order by created_at",
            comprobante_id,
        )
        return [Archivo.model_validate(_row(record)) for record in records]

    async def tipos_por_comprobante(
        self, comprobante_ids: list[UUID]
    ) -> dict[UUID, set[TipoArchivo]]:
        if not comprobante_ids:
            return {}
        records = await self._connection.fetch(
            """
            select comprobante_id, tipo from public.archivos
            where comprobante_id = any($1::uuid[]) and estado = 'subido'
            group by comprobante_id, tipo
            """,
            comprobante_ids,
        )
        tipos: dict[UUID, set[TipoArchivo]] = {}
        for record in records:
            tipos.setdefault(record["comprobante_id"], set()).add(TipoArchivo(record["tipo"]))
        return tipos

    async def list_evidencias(self, company_id: UUID, *, tipo: TipoArchivo) -> list[Archivo]:
        records = await self._connection.fetch(
            """
            select * from public.archivos
            where company_id = $1 and tipo = $2 and comprobante_id is null
            order by created_at desc
            """,
            company_id,
            tipo.value,
        )
        return [Archivo.model_validate(_row(record)) for record in records]

    async def registrar(self, registro: ArchivoRegistro) -> Archivo:
        record = await self._connection.fetchrow(
            """
            insert into public.archivos (
                company_id, comprobante_id, tipo, nombre, mime_type, size_bytes, hash_sha256,
                storage_provider, storage_bucket, storage_key, estado, error_detalle, subido_at
            )
            values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, now())
            returning *
            """,
            registro.company_id,
            registro.comprobante_id,
            registro.tipo.value,
            registro.nombre,
            registro.mime_type,
            registro.size_bytes,
            registro.hash_sha256,
            registro.storage_provider,
            registro.storage_bucket,
            registro.storage_key,
            registro.estado.value,
            registro.error_detalle,
        )
        data = _row(record)
        assert data is not None
        return Archivo.model_validate(data)


class PostgresAutomatizacionLogsRepository:
    """Corridas de la automatización diaria."""

    _COLUMNAS = (
        "id, company_id, created_at, modo, tareas, resultado, duracion_segundos, novedades, "
        "errores, advertencias, detalle, estado, pendientes, created_by"
    )

    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    async def registrar(self, registro: AutomatizacionLogRegistro) -> AutomatizacionLog:
        record = await self._connection.fetchrow(
            f"""
            insert into public.automatizacion_logs (
                company_id, modo, tareas, resultado, duracion_segundos, novedades, errores,
                advertencias, detalle, estado, pendientes, created_by
            )
            values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            returning {self._COLUMNAS}
            """,
            registro.company_id,
            registro.modo,
            registro.tareas,
            registro.resultado,
            registro.duracion_segundos,
            registro.novedades,
            registro.errores,
            registro.advertencias,
            registro.detalle,
            registro.estado,
            registro.pendientes,
            registro.created_by,
        )
        data = _row(record)
        assert data is not None
        return AutomatizacionLog.model_validate(data)

    async def listar(
        self, company_id: UUID, *, resultado: str | None, limit: int, offset: int
    ) -> tuple[list[AutomatizacionLog], int]:
        registros = await self._connection.fetch(
            f"""
            select {self._COLUMNAS}, count(*) over() as total
              from public.automatizacion_logs
             where company_id = $1 and ($2::text is null or resultado = $2)
             order by created_at desc, id desc
             limit $3 offset $4
            """,
            company_id,
            resultado,
            limit,
            offset,
        )
        total = int(registros[0]["total"]) if registros else 0
        items = [AutomatizacionLog.model_validate(_row(r)) for r in registros]
        return items, total


class PostgresBizlinksConsultasRepository:
    """Bitácora de consultas al OSE (topes, cortacircuito y auditoría)."""

    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    async def registrar(self, registro: BizlinksConsultaRegistro) -> BizlinksConsulta:
        record = await self._connection.fetchrow(
            """
            insert into public.bizlinks_consultas (
                company_id, comprobante_id, canal, resultado, http_status, detalle
            )
            values ($1, $2, $3, $4, $5, $6)
            returning id, company_id, comprobante_id, canal, resultado, http_status, detalle,
                      created_at
            """,
            registro.company_id,
            registro.comprobante_id,
            registro.canal,
            registro.resultado,
            registro.http_status,
            registro.detalle,
        )
        data = _row(record)
        assert data is not None
        return BizlinksConsulta.model_validate(data)

    async def ultimo_bloqueo_desde(
        self, company_id: UUID, canal: str, desde: datetime
    ) -> datetime | None:
        return await self._connection.fetchval(
            """
            select max(created_at) from public.bizlinks_consultas
            where company_id = $1 and canal = $2 and resultado = 'bloqueado'
              and created_at >= $3
            """,
            company_id,
            canal,
            desde,
        )

    async def comprobantes_consultados_desde(
        self, company_id: UUID, canal: str, desde: datetime
    ) -> int:
        total = await self._connection.fetchval(
            """
            select count(distinct comprobante_id) from public.bizlinks_consultas
            where company_id = $1 and canal = $2 and created_at >= $3
              and comprobante_id is not null
            """,
            company_id,
            canal,
            desde,
        )
        return int(total or 0)

    async def sin_exito_desde(
        self, company_id: UUID, comprobante_ids: list[UUID], canal: str, desde: datetime
    ) -> set[UUID]:
        if not comprobante_ids:
            return set()
        records = await self._connection.fetch(
            """
            select distinct comprobante_id from public.bizlinks_consultas
            where company_id = $1 and comprobante_id = any($2::uuid[]) and canal = $3
              and created_at >= $4 and resultado = 'sin_resultado'
            """,
            company_id,
            comprobante_ids,
            canal,
            desde,
        )
        return {record["comprobante_id"] for record in records}
