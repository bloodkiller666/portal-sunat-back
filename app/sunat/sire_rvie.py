"""Lectura de la propuesta RVIE (registro de ventas) descargada de SIRE.

SUNAT entrega un ZIP con un TXT de 40 columnas separadas por `|`, en UTF-8 con
BOM (formato verificado contra una descarga real). Solo trae la cabecera de cada
comprobante: no hay líneas de detalle ni XML.

Reglas de lectura:

* Los importes de las notas de crédito llegan negativos; el portal los guarda en
  positivo, igual que cuando se importa el XML (el tipo `07` ya dice que resta).
* `Est. Comp` = 2 (o 0) es un comprobante dado de baja: SUNAT lo informa con
  total 0, así que pasa a estado `Anulado`.
* Las boletas resumidas en un rango (`Nro Final`) no se pueden representar como un
  comprobante individual: se omiten y se cuentan en `omitidos`.

El ZIP viene de un servicio externo, así que se lee con topes de tamaño y de
filas (misma filosofía que `app/services/paquetes.py`).
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from app.core.errors import SunatFaultError

MAX_ENTRADAS_ZIP = 5
MAX_BYTES_TXT = 60 * 1024 * 1024
MAX_FILAS = 20_000

TIPOS_SOPORTADOS = frozenset({"01", "03", "07", "08"})
ESTADOS_ANULADO = frozenset({"0", "2"})

# Cabecera real de la propuesta (verificada con una descarga de producción).
CABECERA = (
    "Ruc",
    "Razon Social",
    "Periodo",
    "CAR SUNAT",
    "Fecha de emisión",
    "Fecha Vcto/Pago",
    "Tipo CP/Doc.",
    "Serie del CDP",
    "Nro CP o Doc. Nro Inicial (Rango)",
    "Nro Final (Rango)",
    "Tipo Doc Identidad",
    "Nro Doc Identidad",
    "Apellidos Nombres/ Razón Social",
    "Valor Facturado Exportación",
    "BI Gravada",
    "Dscto BI",
    "IGV / IPM",
    "Dscto IGV / IPM",
    "Mto Exonerado",
    "Mto Inafecto",
    "ISC",
    "BI Grav IVAP",
    "IVAP",
    "ICBPER",
    "Otros Tributos",
    "Total CP",
    "Moneda",
    "Tipo Cambio",
    "Fecha Emisión Doc Modificado",
    "Tipo CP Modificado",
    "Serie CP Modificado",
    "Nro CP Modificado",
    "ID Proyecto Operadores Atribución",
    "Tipo de Nota",
    "Est. Comp",
    "Valor FOB Embarcado",
    "Valor OP Gratuitas",
    "Tipo Operación",
    "DAM / CP",
    "CLU",
)
# Columnas sin las cuales no se puede construir el comprobante.
_OBLIGATORIAS = (
    "Ruc",
    "Fecha de emisión",
    "Tipo CP/Doc.",
    "Serie del CDP",
    "Nro CP o Doc. Nro Inicial (Rango)",
    "Total CP",
    "Moneda",
    "Est. Comp",
)


@dataclass(frozen=True)
class RegistroSire:
    """Cabecera de un comprobante emitido tal como figura en la propuesta."""

    ruc_emisor: str
    razon_social_emisor: str | None
    tipo_documento: str
    serie: str
    correlativo: int
    fecha_emision: date
    fecha_vencimiento: date | None
    moneda: str
    monto_total: Decimal
    anulado: bool
    receptor_tipo_doc: str | None
    receptor_doc: str | None
    razon_social_receptor: str | None
    # `monto_total` ya viene en la moneda del comprobante; SIRE lo informa en soles.
    tipo_cambio: Decimal | None = None
    monto_pen: Decimal | None = None

    @property
    def ruc_receptor(self) -> str | None:
        """Solo un documento tipo 6 (RUC) es RUC del adquirente."""
        if self.receptor_tipo_doc == "6" and self.receptor_doc:
            return self.receptor_doc
        return None


@dataclass
class ResultadoPropuesta:
    registros: list[Any] = field(default_factory=list)
    omitidos: dict[str, int] = field(default_factory=dict)

    def omitir(self, motivo: str) -> None:
        self.omitidos[motivo] = self.omitidos.get(motivo, 0) + 1

    @property
    def total_omitidos(self) -> int:
        return sum(self.omitidos.values())


def parse_propuesta_zip(
    contenido: bytes,
    *,
    ruc_esperado: str,
    lector: Callable[[str, str, ResultadoPropuesta], None] | None = None,
) -> ResultadoPropuesta:
    """Lee todos los TXT del ZIP y devuelve los comprobantes y lo omitido.

    `lector` permite reutilizar los topes de tamaño con otro formato (el RCE de compras).
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(contenido))
    except zipfile.BadZipFile as exc:
        raise SunatFaultError("SUNAT entregó un archivo ZIP inválido") from exc

    entradas = [info for info in zf.infolist() if not info.is_dir()]
    if len(entradas) > MAX_ENTRADAS_ZIP:
        raise SunatFaultError(
            "El ZIP de SUNAT trae más archivos de lo esperado",
            details={"maximo": MAX_ENTRADAS_ZIP, "recibidos": len(entradas)},
        )

    resultado = ResultadoPropuesta()
    for info in entradas:
        if info.file_size > MAX_BYTES_TXT:
            raise SunatFaultError(
                "El archivo de la propuesta supera el tamaño máximo permitido",
                details={"maximo_bytes": MAX_BYTES_TXT},
            )
        # Se lee con tope real (no se confía en el tamaño declarado en el ZIP).
        with zf.open(info) as flujo:
            datos = flujo.read(MAX_BYTES_TXT + 1)
        if len(datos) > MAX_BYTES_TXT:
            raise SunatFaultError(
                "El archivo de la propuesta supera el tamaño máximo permitido",
                details={"maximo_bytes": MAX_BYTES_TXT},
            )
        (lector or _parse_txt)(datos.decode("utf-8-sig", errors="replace"), ruc_esperado, resultado)
    return resultado


def _parse_txt(texto: str, ruc_esperado: str, resultado: ResultadoPropuesta) -> None:
    lineas = texto.splitlines()
    if not lineas:
        return
    cabecera = [columna.strip() for columna in lineas[0].split("|")]
    faltantes = [c for c in _OBLIGATORIAS if c not in cabecera]
    if faltantes:
        raise SunatFaultError(
            "El formato de la propuesta de SUNAT cambió: faltan columnas",
            details={"faltantes": faltantes},
        )
    idx = {nombre: i for i, nombre in enumerate(cabecera)}

    for linea in lineas[1:]:
        if not linea.strip():
            continue
        if len(resultado.registros) + resultado.total_omitidos >= MAX_FILAS:
            raise SunatFaultError(
                "La propuesta supera el máximo de filas permitido",
                details={"maximo": MAX_FILAS},
            )
        celdas = linea.split("|")
        if len(celdas) < len(cabecera):
            resultado.omitir("fila_incompleta")
            continue

        def valor(nombre: str, _celdas: list[str] = celdas) -> str:
            posicion = idx.get(nombre)
            return _celdas[posicion].strip() if posicion is not None else ""

        registro = _fila_a_registro(valor, ruc_esperado, resultado)
        if registro is not None:
            resultado.registros.append(registro)


def _fila_a_registro(
    valor, ruc_esperado: str, resultado: ResultadoPropuesta
) -> RegistroSire | None:
    if valor("Ruc") != ruc_esperado:
        # Un RUC distinto jamás debe mezclarse con la empresa que sincroniza.
        raise SunatFaultError(
            "La propuesta pertenece a otro RUC",
            details={"ruc_empresa": ruc_esperado},
        )
    tipo = valor("Tipo CP/Doc.")
    if tipo not in TIPOS_SOPORTADOS:
        resultado.omitir("tipo_no_soportado")
        return None
    if valor("Nro Final (Rango)"):
        resultado.omitir("boletas_en_rango")
        return None

    serie = valor("Serie del CDP").upper()
    nro = valor("Nro CP o Doc. Nro Inicial (Rango)")
    if not serie or not nro.isdigit() or int(nro) <= 0:
        resultado.omitir("numeracion_invalida")
        return None
    emision = _fecha(valor("Fecha de emisión"))
    if emision is None:
        resultado.omitir("fecha_invalida")
        return None
    try:
        total = abs(Decimal(valor("Total CP") or "0"))
    except InvalidOperation:
        resultado.omitir("importe_invalido")
        return None

    moneda = (valor("Moneda") or "PEN").upper()[:3]
    anulado = valor("Est. Comp") in ESTADOS_ANULADO
    tipo_cambio = _decimal(valor("Tipo Cambio"))
    monto, monto_pen = monto_en_moneda(total, moneda, tipo_cambio)
    return RegistroSire(
        ruc_emisor=valor("Ruc"),
        razon_social_emisor=valor("Razon Social") or None,
        tipo_documento=tipo,
        serie=serie,
        correlativo=int(nro),
        fecha_emision=emision,
        fecha_vencimiento=_fecha(valor("Fecha Vcto/Pago")),
        moneda=moneda,
        monto_total=monto,
        anulado=anulado,
        receptor_tipo_doc=valor("Tipo Doc Identidad") or None,
        receptor_doc=valor("Nro Doc Identidad") or None,
        razon_social_receptor=valor("Apellidos Nombres/ Razón Social") or None,
        tipo_cambio=tipo_cambio,
        monto_pen=monto_pen,
    )


def _decimal(texto: str) -> Decimal | None:
    try:
        return Decimal(texto) if texto else None
    except InvalidOperation:
        return None


def monto_en_moneda(
    total_sire: Decimal, moneda: str, tipo_cambio: Decimal | None
) -> tuple[Decimal, Decimal | None]:
    """Devuelve `(monto en la moneda del comprobante, total en soles que informó SIRE)`.

    SIRE entrega `Total CP` en SOLES (convertido con `Tipo Cambio`) aunque `Moneda` diga USD:
    comprobado con una factura en dólares, que Bizlinks solo reconoce con `total / tipo de
    cambio`. Sin tipo de cambio válido no se puede convertir: se conserva el total tal cual.
    """
    if moneda == "PEN":
        return total_sire, total_sire
    if tipo_cambio is None or tipo_cambio <= 0:
        return total_sire, None
    return (total_sire / tipo_cambio).quantize(Decimal("0.01"), ROUND_HALF_UP), total_sire


def _fecha(texto: str) -> date | None:
    if not texto:
        return None
    try:
        return datetime.strptime(texto, "%d/%m/%Y").date()
    except ValueError:
        return None
