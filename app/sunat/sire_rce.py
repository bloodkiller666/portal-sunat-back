"""Lectura de la propuesta RCE (registro de compras) descargada de SIRE.

SUNAT entrega un ZIP con un TXT de 80 columnas separadas por `|`, en UTF-8 sin BOM (formato
verificado con una descarga real de TDM, periodo 202609). Solo trae la cabecera de cada
comprobante recibido: no hay líneas de detalle ni XML. Las columnas `CLU1…CLU39` son de uso
interno de SUNAT y se ignoran.

Diferencias con el RVIE de ventas (`sire_rvie.py`):

* El **emisor es el proveedor**: sus datos van en `Tipo Doc Identidad`, `Nro Doc Identidad` y
  `Apellidos Nombres/ Razón Social`. La columna `RUC` es la de la empresa que compra (el
  adquirente) y las columnas «Apellidos y Nombres o Razón social» (primera) también.
* Los nombres de columna traen tildes y espacios dobles irregulares: se comparan normalizados.
* Igual que en ventas, `Total CP` está en soles y los importes de las notas de crédito vienen
  negativos (se guardan en positivo; el tipo `07` ya dice que resta).
* Solo se guardan los tipos 01/03/07/08 con proveedor identificado por RUC (tipo de documento 6).
  Lo demás (p. ej. el tipo 50, declaración aduanera) se omite y se cuenta en `omitidos`.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from app.core.errors import SunatFaultError
from app.sunat.sire_rvie import (
    ESTADOS_ANULADO,
    MAX_FILAS,
    TIPOS_SOPORTADOS,
    ResultadoPropuesta,
    _fecha,
    monto_en_moneda,
    parse_propuesta_zip,
    tipo_cambio_valido,
)

# Columnas (ya normalizadas) sin las cuales no se puede construir el comprobante.
_OBLIGATORIAS = (
    "ruc",
    "fecha de emision",
    "tipo cp/doc.",
    "serie del cdp",
    "nro cp o doc. nro inicial (rango)",
    "tipo doc identidad",
    "nro doc identidad",
    "total cp",
    "moneda",
    "est. comp.",
)


@dataclass(frozen=True)
class RegistroRCE:
    """Cabecera de un comprobante recibido tal como figura en la propuesta del RCE."""

    ruc_adquirente: str
    ruc_proveedor: str
    razon_social_proveedor: str | None
    proveedor_tipo_doc: str | None
    tipo_documento: str
    serie: str
    correlativo: int
    fecha_emision: date
    fecha_vencimiento: date | None
    moneda: str
    monto_total: Decimal
    anulado: bool
    tipo_cambio: Decimal | None = None
    monto_pen: Decimal | None = None


def _normalizar(nombre: str) -> str:
    """Minúsculas, sin tildes y con espacios simples: la cabecera de SUNAT es irregular."""
    sin_tildes = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode()
    return " ".join(sin_tildes.lower().split())


def parse_propuesta_rce(contenido: bytes, *, ruc_esperado: str) -> ResultadoPropuesta:
    """Lee todos los TXT del ZIP del RCE y devuelve los comprobantes y lo omitido."""
    return parse_propuesta_zip(contenido, ruc_esperado=ruc_esperado, lector=_parse_txt)


def _parse_txt(texto: str, ruc_esperado: str, resultado: ResultadoPropuesta) -> None:
    lineas = texto.splitlines()
    if not lineas:
        return
    cabecera = [_normalizar(columna) for columna in lineas[0].split("|")]
    faltantes = [c for c in _OBLIGATORIAS if c not in cabecera]
    if faltantes:
        raise SunatFaultError(
            "El formato de la propuesta de compras de SUNAT cambió: faltan columnas",
            details={"faltantes": faltantes},
        )
    # La razón social del proveedor es la que va después del documento de identidad.
    posicion_nombre = cabecera.index("nro doc identidad") + 1
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

        registro = _fila_a_registro(valor, celdas[posicion_nombre].strip(), ruc_esperado, resultado)
        if registro is not None:
            resultado.registros.append(registro)


def _fila_a_registro(
    valor, razon_social: str, ruc_esperado: str, resultado: ResultadoPropuesta
) -> RegistroRCE | None:
    if valor("ruc") != ruc_esperado:
        # Un RUC distinto jamás debe mezclarse con la empresa que sincroniza.
        raise SunatFaultError(
            "La propuesta pertenece a otro RUC",
            details={"ruc_empresa": ruc_esperado},
        )
    tipo = valor("tipo cp/doc.")
    if tipo not in TIPOS_SOPORTADOS:
        resultado.omitir("tipo_no_soportado")
        return None
    if valor("nro final (rango)"):
        resultado.omitir("boletas_en_rango")
        return None
    proveedor_tipo_doc = valor("tipo doc identidad")
    proveedor = valor("nro doc identidad")
    if proveedor_tipo_doc != "6" or not (proveedor.isdigit() and len(proveedor) == 11):
        # La base exige un RUC de 11 dígitos para el emisor.
        resultado.omitir("proveedor_sin_ruc")
        return None

    serie = valor("serie del cdp").upper()
    nro = valor("nro cp o doc. nro inicial (rango)")
    if not serie or not nro.isdigit() or int(nro) <= 0:
        resultado.omitir("numeracion_invalida")
        return None
    emision = _fecha(valor("fecha de emision"))
    if emision is None:
        resultado.omitir("fecha_invalida")
        return None
    try:
        total = abs(Decimal(valor("total cp") or "0"))
    except InvalidOperation:
        resultado.omitir("importe_invalido")
        return None

    moneda = (valor("moneda") or "PEN").upper()[:3]
    tipo_cambio = tipo_cambio_valido(valor("tipo de cambio"))
    monto, monto_pen = monto_en_moneda(total, moneda, tipo_cambio)
    return RegistroRCE(
        ruc_adquirente=ruc_esperado,
        ruc_proveedor=proveedor,
        razon_social_proveedor=razon_social or None,
        proveedor_tipo_doc=proveedor_tipo_doc,
        tipo_documento=tipo,
        serie=serie,
        correlativo=int(nro),
        fecha_emision=emision,
        fecha_vencimiento=_fecha(valor("fecha vcto/pago")),
        moneda=moneda,
        monto_total=monto,
        anulado=valor("est. comp.") in ESTADOS_ANULADO,
        tipo_cambio=tipo_cambio,
        monto_pen=monto_pen,
    )
