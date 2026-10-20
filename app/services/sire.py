"""Sincronización de las ventas reales desde SIRE (propuesta RVIE).

Pobla `comprobantes` (origen `emitido`) con lo que SUNAT tiene registrado de la
empresa en un periodo. Es de SOLO LECTURA hacia SUNAT y **idempotente**: lo que
ya existe (por la restricción única del comprobante) se respeta tal cual, así que
sincronizar dos veces el mismo periodo no duplica ni pisa nada.

El flujo de SUNAT es asíncrono por tickets. Una petición HTTP espera un tiempo
acotado (`ESPERA_MAXIMA_SEGUNDOS`); si el ticket aún no termina responde
`en_proceso` con el número de ticket, y el cliente vuelve a llamar con ese ticket
(sin pedir otra propuesta).

Los registros de SIRE se guardan con ambiente `prod` —SIRE solo existe en
producción— y `fuente=sire`. No traen ítems; si luego se importa su XML, el XML
completa el registro (ver `import_comprobante_xml`).
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

from app.core.concurrency import run_blocking
from app.core.config import Settings
from app.core.enums import (
    EstadoComprobante,
    OrigenComprobante,
    SunatAmbiente,
    TipoArchivo,
)
from app.core.errors import AppError, ConflictError, SunatFaultError
from app.core.logging import get_logger
from app.core.security import AuthenticatedUser
from app.db.models import CompanyMember, ComprobanteRegistro
from app.db.repositories.base import FiltrosListado, UnitOfWork
from app.secrets.base import CompanyCredentials, SecretsProvider
from app.services.archivos import to_archivo_out
from app.services.bizlinks import LIMITE_MAXIMO as LIMITE_RESPALDOS
from app.services.bizlinks import sincronizar_respaldos
from app.services.results import ArchivoOut, BizlinksSyncOut, EvidenciasPage, SireSyncOut
from app.services.store import guardar_archivo_de_empresa_sin_fallar
from app.sunat.sire import (
    SireClient,
    SirePeriodoSinDatosError,
)
from app.sunat.sire_rvie import RegistroSire, ResultadoPropuesta, parse_propuesta_zip

SireClientFactory = Callable[[CompanyCredentials, Settings], SireClient]

ESPERA_MAXIMA_SEGUNDOS = 25.0
INTERVALO_SEGUNDOS = 2.0
MAX_ARCHIVOS_TICKET = 5

_PERIODO_RE = re.compile(r"^(20\d{2})(0[1-9]|1[0-2])$")
_TICKET_RE = re.compile(r"^\d{8,20}$")

logger = get_logger(__name__)


class PeriodoInvalidoError(AppError):
    status_code = 422
    code = "periodo_invalido"
    default_message = "El periodo debe tener el formato AAAAMM y no ser futuro"


class PeriodoYaCargadoError(ConflictError):
    code = "periodo_ya_cargado"


def validar_periodo(periodo: str, hoy: date | None = None) -> str:
    coincidencia = _PERIODO_RE.match(periodo)
    if coincidencia is None:
        raise PeriodoInvalidoError(details={"periodo": periodo})
    hoy = hoy or datetime.now(tz=UTC).date()
    if (int(coincidencia.group(1)), int(coincidencia.group(2))) > (hoy.year, hoy.month):
        raise PeriodoInvalidoError("El periodo no puede ser futuro", details={"periodo": periodo})
    return periodo


async def sincronizar_ventas(
    *,
    uow: UnitOfWork,
    settings: Settings,
    secrets_provider: SecretsProvider,
    client_factory: SireClientFactory,
    user: AuthenticatedUser,
    access: CompanyMember,
    periodo: str,
    ticket: str | None = None,
    traer_respaldos: bool = False,
    forzar: bool = False,
    bizlinks_client_factory: Callable[..., object] | None = None,
) -> SireSyncOut:
    """Sincroniza el RVIE del periodo (AAAAMM) con los comprobantes de la empresa.

    Con `traer_respaldos` y una fábrica de Bizlinks, después de registrar las ventas se
    recupera el XML firmado y el CDR que falten de esos comprobantes. Un fallo de
    Bizlinks no invalida la sincronización: se informa en `respaldos_error`.
    """
    periodo = validar_periodo(periodo)
    if ticket is not None and not _TICKET_RE.match(ticket):
        raise PeriodoInvalidoError("El número de ticket no es válido", details={"ticket": ticket})

    ruc = access.company.ruc
    if ticket is None and not forzar:
        await _rechazar_si_ya_cargado(uow, access, periodo)
    credenciales = secrets_provider.get_company_credentials(ruc)
    cliente = client_factory(credenciales, settings)

    if ticket is None:
        try:
            ticket = await run_blocking(cliente.solicitar_propuesta, periodo)
        except SirePeriodoSinDatosError:
            return SireSyncOut(
                periodo=periodo,
                estado="sin_datos",
                mensaje="SUNAT no tiene comprobantes en la propuesta de ese periodo",
            )

    inicio = time.monotonic()
    while True:
        estado = await run_blocking(cliente.consultar_ticket, ticket, periodo)
        if estado.terminado:
            break
        if estado.con_errores:
            raise SunatFaultError(
                "SUNAT terminó el ticket con errores", details={"ticket": ticket}
            )
        if not estado.en_proceso:
            raise SunatFaultError(
                "SUNAT devolvió un estado de ticket desconocido",
                details={"ticket": ticket, "codigo": estado.codigo_estado},
            )
        if time.monotonic() - inicio >= ESPERA_MAXIMA_SEGUNDOS:
            return SireSyncOut(
                periodo=periodo,
                ticket=ticket,
                estado="en_proceso",
                mensaje="SUNAT sigue generando el archivo: vuelva a consultar con este ticket",
            )
        await asyncio.sleep(INTERVALO_SEGUNDOS)

    if not estado.archivos:
        return SireSyncOut(
            periodo=periodo,
            ticket=ticket,
            estado="sin_datos",
            mensaje="El ticket terminó sin archivos: el periodo no tiene comprobantes",
        )

    propuesta = ResultadoPropuesta()
    evidencias: list[ArchivoOut] = []
    for parte, archivo in enumerate(estado.archivos[:MAX_ARCHIVOS_TICKET], start=1):
        contenido = await run_blocking(cliente.descargar_archivo, estado, archivo)
        parcial = parse_propuesta_zip(contenido, ruc_esperado=ruc)
        propuesta.registros.extend(parcial.registros)
        for motivo, cantidad in parcial.omitidos.items():
            propuesta.omitidos[motivo] = propuesta.omitidos.get(motivo, 0) + cantidad

        # Evidencia de auditoría: el ZIP exacto que entregó SUNAT, con su SHA-256.
        # Se archiva solo si se pudo leer (un ZIP inválido no es evidencia útil) y un
        # fallo de R2 no impide registrar los comprobantes.
        guardado = await guardar_archivo_de_empresa_sin_fallar(
            uow=uow,
            settings=settings,
            company_id=access.company_id,
            tipo=TipoArchivo.zip,
            file_bytes=contenido,
            filename=f"{ruc}-RVIE-{periodo}-{ticket}-{parte}.zip",
            folder_path=f"sire/{ruc}/{periodo}",
        )
        if guardado is not None:
            evidencias.append(to_archivo_out(guardado))

    nuevos = existentes = anulados = 0
    for registro in propuesta.registros:
        creado = await uow.comprobantes.create_if_absent(
            _a_registro(registro, access=access, user=user)
        )
        if creado is None:
            existentes += 1
            continue
        nuevos += 1
        anulados += 1 if registro.anulado else 0

    logger.info(
        "SIRE %s: %s nuevos, %s ya existían, %s omitidos",
        periodo,
        nuevos,
        existentes,
        propuesta.total_omitidos,
    )

    respaldos: BizlinksSyncOut | None = None
    respaldos_error: str | None = None
    if traer_respaldos and nuevos and bizlinks_client_factory is not None:
        try:
            respaldos = await sincronizar_respaldos(
                uow=uow,
                settings=settings,
                secrets_provider=secrets_provider,
                client_factory=bizlinks_client_factory,
                access=access,
                limit=min(nuevos, LIMITE_RESPALDOS),
            )
        except AppError as exc:
            # Los comprobantes ya quedaron registrados: el respaldo es un extra.
            respaldos_error = exc.message

    return SireSyncOut(
        periodo=periodo,
        ticket=ticket,
        estado="completado",
        total_filas=len(propuesta.registros) + propuesta.total_omitidos,
        nuevos=nuevos,
        ya_existian=existentes,
        anulados=anulados,
        omitidos=propuesta.omitidos,
        evidencias=evidencias,
        respaldos=respaldos,
        respaldos_error=respaldos_error,
        mensaje=f"{nuevos} comprobantes nuevos, {existentes} ya estaban registrados",
    )


async def _rechazar_si_ya_cargado(uow: UnitOfWork, access: CompanyMember, periodo: str) -> None:
    """Rechaza la sincronización si el periodo ya tiene ventas traídas de SIRE.

    Se valida antes de llamar a SUNAT (ahorra el ticket). Retomar un ticket `en_proceso` no
    pasa por aquí: sus filas aún no se registraron. `forzar` permite volver a sincronizar
    (sigue siendo idempotente: solo agrega lo que falte).
    """
    anio, mes = int(periodo[:4]), int(periodo[4:])
    desde = date(anio, mes, 1)
    hasta = date(anio + (mes == 12), mes % 12 + 1, 1) - timedelta(days=1)
    pagina = await uow.comprobantes.list_for_company(
        access.company_id,
        FiltrosListado(
            origen=OrigenComprobante.emitido, fuente="sire", fecha_desde=desde, fecha_hasta=hasta
        ),
        limit=1,
        contar=True,
    )
    if pagina.total:
        raise PeriodoYaCargadoError(
            f"El periodo {periodo} ya está cargado ({pagina.total} comprobantes de SIRE). "
            "No se volvió a sincronizar.",
            details={"periodo": periodo, "cargados": pagina.total},
        )


def _a_registro(
    dato: RegistroSire, *, access: CompanyMember, user: AuthenticatedUser
) -> ComprobanteRegistro:
    numero = f"{dato.correlativo:08d}"
    return ComprobanteRegistro(
        company_id=access.company_id,
        origen=OrigenComprobante.emitido,
        ruc_emisor=dato.ruc_emisor,
        ruc_receptor=dato.ruc_receptor,
        receptor_doc=dato.receptor_doc,
        razon_social_emisor=dato.razon_social_emisor,
        razon_social_receptor=dato.razon_social_receptor,
        tipo_documento=dato.tipo_documento,
        serie=dato.serie,
        correlativo=dato.correlativo,
        numero=numero,
        nombre_archivo=f"{dato.ruc_emisor}-{dato.tipo_documento}-{dato.serie}-{numero}",
        fecha_emision=dato.fecha_emision,
        fecha_vencimiento=dato.fecha_vencimiento,
        moneda=dato.moneda,
        monto_total=dato.monto_total,
        # Si SUNAT lo lista en su propuesta, lo recibió: Aprobado (o dado de baja).
        estado=EstadoComprobante.anulado if dato.anulado else EstadoComprobante.aprobado,
        # SIRE solo existe en producción: estos comprobantes son reales.
        sunat_ambiente=SunatAmbiente.prod,
        created_by=user.id,
        fuente="sire",
    )


async def listar_evidencias(
    *, uow: UnitOfWork, user: AuthenticatedUser, access: CompanyMember
) -> EvidenciasPage:
    """ZIP originales de SIRE archivados para la empresa (cualquier miembro)."""
    archivos = await uow.archivos.list_evidencias(access.company_id, tipo=TipoArchivo.zip)
    items = [to_archivo_out(archivo) for archivo in archivos]
    return EvidenciasPage(items=items, total=len(items))
