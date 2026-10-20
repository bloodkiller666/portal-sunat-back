"""Importación masiva de N° SAP: lo que no está en el portal se omite sin error."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.db.models import Company
from app.services.sap import normalizar_fila
from tests.conftest import RUC_PRINCIPAL
from tests.fakes import FakeUnitOfWork, construir_comprobante

URL = f"/api/v1/companies/{RUC_PRINCIPAL}/comprobantes/import-sap"


def _agregar(uow: FakeUnitOfWork, empresa: Company, **datos):
    comprobante = construir_comprobante(empresa.id, ruc=RUC_PRINCIPAL, **datos)
    uow.comprobantes.comprobantes[uuid4()] = comprobante
    return comprobante


def _sap(uow: FakeUnitOfWork, correlativo: int, serie: str = "F001") -> str | None:
    return next(
        c.n_sap
        for c in uow.comprobantes.comprobantes.values()
        if c.serie == serie and c.correlativo == correlativo
    )


@pytest.mark.parametrize(
    ("documento", "esperado"),
    [
        ("F001-123", ("F001", 123)),
        ("f001-00000123", ("F001", 123)),
        (" F001 123 ", ("F001", 123)),
        ("B002/7", ("B002", 7)),
        ("F001-0", None),
        ("F001", None),
        ("123", None),
        ("", None),
        (None, None),
    ],
)
def test_normaliza_el_documento(documento: str | None, esperado: tuple[str, int] | None) -> None:
    fila = normalizar_fila(documento, None, "4500")
    assert (None if fila is None else (fila.serie, fila.correlativo)) == esperado


def test_una_fila_sin_n_sap_o_con_ruc_malo() -> None:
    assert normalizar_fila("F001-1", None, "  ") is None
    assert normalizar_fila("F001-1", None, "x" * 51) is None
    # Un RUC ilegible no descarta la fila: simplemente no afina el emparejamiento.
    fila = normalizar_fila("F001-1", "abc", "4500")
    assert fila is not None and fila.ruc is None


def test_asigna_los_que_existen_y_omite_el_resto_sin_error(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    _agregar(uow, empresa_principal, correlativo=1)
    _agregar(uow, empresa_principal, correlativo=2)

    respuesta = client.post(
        URL,
        headers=auth,
        json={
            "filas": [
                {"documento": "F001-1", "n_sap": "4500001"},
                {"documento": "F001-00000002", "n_sap": " 4500002 "},
                {"documento": "F001-999", "n_sap": "4500999"},  # no está en el portal
                {"documento": "basura", "n_sap": "1"},  # ilegible
                {"documento": "F001-2", "n_sap": ""},  # sin N° SAP
            ]
        },
    )

    assert respuesta.status_code == 200
    assert respuesta.json() == {
        "ruc": RUC_PRINCIPAL,
        "recibidas": 5,
        "actualizadas": 2,
        "no_encontradas": 1,
        "invalidas": 2,
    }
    assert _sap(uow, 1) == "4500001"
    assert _sap(uow, 2) == "4500002"


def test_el_ruc_afina_el_emparejamiento(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork, empresa_principal: Company
) -> None:
    _agregar(uow, empresa_principal, correlativo=1)

    otro = client.post(
        URL,
        headers=auth,
        json={"filas": [{"documento": "F001-1", "ruc": "20999999999", "n_sap": "9"}]},
    ).json()
    propio = client.post(
        URL,
        headers=auth,
        json={"filas": [{"documento": "F001-1", "ruc": RUC_PRINCIPAL, "n_sap": "8"}]},
    ).json()

    assert (otro["actualizadas"], otro["no_encontradas"]) == (0, 1)
    assert propio["actualizadas"] == 1
    assert _sap(uow, 1) == "8"


def test_no_toca_comprobantes_de_otra_empresa(
    client: TestClient, auth: dict[str, str], uow: FakeUnitOfWork
) -> None:
    ajeno = construir_comprobante(uuid4(), ruc="20111111111", correlativo=1)
    uow.comprobantes.comprobantes[uuid4()] = ajeno

    respuesta = client.post(
        URL, headers=auth, json={"filas": [{"documento": "F001-1", "n_sap": "1"}]}
    )

    assert respuesta.json()["actualizadas"] == 0
    assert _sap(uow, 1) is None


def test_lista_vacia_no_falla(client: TestClient, auth: dict[str, str]) -> None:
    respuesta = client.post(URL, headers=auth, json={"filas": []})
    assert respuesta.status_code == 200
    assert respuesta.json()["recibidas"] == 0


def test_el_lector_no_puede_importar(client: TestClient, auth_lector: dict[str, str]) -> None:
    respuesta = client.post(URL, headers=auth_lector, json={"filas": []})
    assert respuesta.status_code == 403


def test_exige_token(client: TestClient) -> None:
    assert client.post(URL, json={"filas": []}).status_code == 401
