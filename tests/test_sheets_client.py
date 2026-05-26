"""Tests del cliente fino sobre gspread (``app.services.sheets_client``).

Estrategia:

- Cero red. Cero gspread real. Inyectamos un ``_FakeWorksheet`` que
  implementa el ``WorksheetLike`` Protocol y graba todas las llamadas
  para inspeccionarlas. Hooks para inyectar excepciones por metodo.

- El ``worksheet_factory`` del ``SheetsClient`` se pasa explicito en
  cada test: un callable que devuelve el fake (o lanza, segun el caso).

- No cubrimos ``_default_worksheet_factory``: importa gspread y abre
  cuenta de servicio. Se valida con smoke manual contra la hoja real.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from app.services.sheets_client import (
    SheetsClient,
    SheetsClientError,
    WorksheetLike,
)
from app.services.sheets_formatter import HEADERS


# ---------------------------------------------------------------------------
# Double del Worksheet
# ---------------------------------------------------------------------------


class _UpdateCall:
    """Captura los kwargs de una llamada a ``update``."""

    def __init__(self, range_name: str, values: list[list[str]], **kwargs: Any) -> None:
        self.range_name = range_name
        self.values = values
        self.kwargs = kwargs


class _BatchUpdateCall:
    def __init__(self, data: list[dict[str, Any]], **kwargs: Any) -> None:
        self.data = data
        self.kwargs = kwargs


class _AppendRowsCall:
    def __init__(self, values: list[list[str]], **kwargs: Any) -> None:
        self.values = values
        self.kwargs = kwargs


class _FakeWorksheet:
    """Double con la superficie minima que usa el cliente.

    Estado:
    - ``column_a``: la columna A precargada (incluye cabecera si la hay).

    Hooks de inyeccion de errores: cualquiera de los cuatro
    ``*_error`` se lanza la **primera** vez que se invoca ese metodo.
    Util para simular fallos de red puntuales.
    """

    def __init__(
        self,
        *,
        column_a: list[str] | None = None,
        col_values_error: Exception | None = None,
        update_error: Exception | None = None,
        batch_update_error: Exception | None = None,
        append_rows_error: Exception | None = None,
    ) -> None:
        self.column_a: list[str] = list(column_a or [])
        self._col_values_error = col_values_error
        self._update_error = update_error
        self._batch_update_error = batch_update_error
        self._append_rows_error = append_rows_error

        self.col_values_calls: list[int] = []
        self.update_calls: list[_UpdateCall] = []
        self.batch_update_calls: list[_BatchUpdateCall] = []
        self.append_rows_calls: list[_AppendRowsCall] = []

    def col_values(self, col: int) -> list[str]:
        self.col_values_calls.append(col)
        if self._col_values_error is not None:
            exc, self._col_values_error = self._col_values_error, None
            raise exc
        if col != 1:
            return []
        return list(self.column_a)

    def update(self, range_name: str, values: list[list[str]], **kwargs: Any) -> None:
        self.update_calls.append(_UpdateCall(range_name, values, **kwargs))
        if self._update_error is not None:
            exc, self._update_error = self._update_error, None
            raise exc

    def batch_update(self, data: list[dict[str, Any]], **kwargs: Any) -> None:
        self.batch_update_calls.append(_BatchUpdateCall(data, **kwargs))
        if self._batch_update_error is not None:
            exc, self._batch_update_error = self._batch_update_error, None
            raise exc

    def append_rows(
        self, values: list[list[str]], value_input_option: str = "RAW", **kwargs: Any
    ) -> None:
        merged = {"value_input_option": value_input_option, **kwargs}
        self.append_rows_calls.append(_AppendRowsCall(values, **merged))
        if self._append_rows_error is not None:
            exc, self._append_rows_error = self._append_rows_error, None
            raise exc


def _make_client(
    *,
    factory_result: WorksheetLike | None = None,
    factory_error: Exception | None = None,
) -> tuple[SheetsClient, list[int]]:
    """Construye un ``SheetsClient`` con factory inyectada.

    Devuelve (client, factory_call_count) — el contador es una lista de
    un elemento para que el closure pueda mutar.
    """
    calls: list[int] = []

    def factory() -> WorksheetLike:
        calls.append(1)
        if factory_error is not None:
            raise factory_error
        assert factory_result is not None, "test bug: factory_result requerido"
        return factory_result

    client = SheetsClient(
        spreadsheet_id="fake-id",
        credentials_path="/no/existe.json",
        worksheet_name="Tickets",
        worksheet_factory=factory,
    )
    return client, calls


# ---------------------------------------------------------------------------
# read_state — hoja vacia
# ---------------------------------------------------------------------------


def test_read_state_hoja_vacia_escribe_headers_y_mapa_vacio():
    ws = _FakeWorksheet(column_a=[])
    client, _ = _make_client(factory_result=ws)

    out_ws, mapa = client.read_state()

    assert out_ws is ws
    assert mapa == {}
    # Escribio cabeceras en A1.
    assert len(ws.update_calls) == 1
    call = ws.update_calls[0]
    assert call.range_name == "A1"
    assert call.values == [list(HEADERS)]
    # USER_ENTERED es critico para que TRUE/FALSE y numeros se interpreten.
    assert call.kwargs.get("value_input_option") == "USER_ENTERED"


# ---------------------------------------------------------------------------
# read_state — hoja con headers correctos
# ---------------------------------------------------------------------------


def test_read_state_hoja_con_headers_y_dos_filas_construye_mapa():
    ws = _FakeWorksheet(
        column_a=[HEADERS[0], "TLY-2026-0001", "TLY-2026-0002"]
    )
    client, _ = _make_client(factory_result=ws)

    _, mapa = client.read_state()

    # 1-based, fila 1 es cabecera, fila 2 primer ticket.
    assert mapa == {"TLY-2026-0001": 2, "TLY-2026-0002": 3}
    # No reescribe headers si ya estan.
    assert ws.update_calls == []


def test_read_state_filas_vacias_intercaladas_se_ignoran():
    # Caso real: el operador borro una fila por la mitad. Queremos que
    # el mapa siga teniendo numeros de fila correctos para los IDs
    # restantes (la fila vacia se salta).
    ws = _FakeWorksheet(
        column_a=[HEADERS[0], "TLY-2026-0001", "", "TLY-2026-0003"]
    )
    client, _ = _make_client(factory_result=ws)

    _, mapa = client.read_state()

    assert mapa == {"TLY-2026-0001": 2, "TLY-2026-0003": 4}


# ---------------------------------------------------------------------------
# read_state — primera celda inesperada
# ---------------------------------------------------------------------------


def test_read_state_primera_celda_renombrada_loguea_warning_y_no_reescribe(
    caplog: pytest.LogCaptureFixture,
):
    ws = _FakeWorksheet(
        column_a=["id_ticket", "TLY-2026-0001"]  # operador renombro
    )
    client, _ = _make_client(factory_result=ws)

    with caplog.at_level(logging.WARNING, logger="app.services.sheets_client"):
        _, mapa = client.read_state()

    # No sobreescribimos cabeceras si el operador las ha tocado.
    assert ws.update_calls == []
    # Pero seguimos construyendo el mapa: la fila 2 tiene un ID valido.
    assert mapa == {"TLY-2026-0001": 2}
    # Logueamos warning visible para que el operador lo vea en revision.
    assert any(
        "primera celda inesperada" in rec.message for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# read_state — errores
# ---------------------------------------------------------------------------


def test_read_state_factory_lanza_se_envuelve_en_sheets_client_error():
    boom = RuntimeError("credenciales rotas")
    client, _ = _make_client(factory_error=boom)

    with pytest.raises(SheetsClientError) as exc_info:
        client.read_state()

    assert "abrir worksheet fallo" in str(exc_info.value)
    # La causa original debe quedar enlazada para diagnostico.
    assert exc_info.value.__cause__ is boom


def test_read_state_factory_lanza_sheets_client_error_se_repropaga_tal_cual():
    # Si la factory ya devuelve un SheetsClientError (caso tipico del
    # default factory cuando faltan credenciales), no debemos
    # re-envolverlo: rompiendo el chain perderiamos el mensaje original.
    original = SheetsClientError("credenciales no encontradas en '/foo'")
    client, _ = _make_client(factory_error=original)

    with pytest.raises(SheetsClientError) as exc_info:
        client.read_state()

    assert exc_info.value is original


def test_read_state_col_values_lanza_se_envuelve():
    ws = _FakeWorksheet(col_values_error=RuntimeError("API quota"))
    client, _ = _make_client(factory_result=ws)

    with pytest.raises(SheetsClientError) as exc_info:
        client.read_state()

    assert "leer columna A fallo" in str(exc_info.value)


def test_read_state_update_lanza_al_escribir_headers_se_envuelve():
    ws = _FakeWorksheet(column_a=[], update_error=RuntimeError("read-only"))
    client, _ = _make_client(factory_result=ws)

    with pytest.raises(SheetsClientError) as exc_info:
        client.read_state()

    assert "escribir cabeceras fallo" in str(exc_info.value)


# ---------------------------------------------------------------------------
# apply_changes
# ---------------------------------------------------------------------------


def _row(tid: str) -> list[str]:
    """Fila minima: solo el ID. Suficiente para tests del cliente."""
    return [tid] + [""] * (len(HEADERS) - 1)


def test_apply_changes_sin_nada_no_llama_a_la_api():
    ws = _FakeWorksheet()
    client, _ = _make_client(factory_result=ws)

    client.apply_changes(ws, updates=[], appends=[])

    assert ws.batch_update_calls == []
    assert ws.append_rows_calls == []


def test_apply_changes_solo_updates_un_unico_batch_update():
    ws = _FakeWorksheet()
    client, _ = _make_client(factory_result=ws)

    client.apply_changes(
        ws,
        updates=[(2, _row("TLY-2026-0001")), (5, _row("TLY-2026-0002"))],
        appends=[],
    )

    # Una sola llamada HTTP para ambos updates.
    assert len(ws.batch_update_calls) == 1
    call = ws.batch_update_calls[0]
    assert call.data == [
        {"range": "A2", "values": [_row("TLY-2026-0001")]},
        {"range": "A5", "values": [_row("TLY-2026-0002")]},
    ]
    assert call.kwargs.get("value_input_option") == "USER_ENTERED"
    # No hubo appends.
    assert ws.append_rows_calls == []


def test_apply_changes_solo_appends_un_unico_append_rows():
    ws = _FakeWorksheet()
    client, _ = _make_client(factory_result=ws)

    client.apply_changes(
        ws,
        updates=[],
        appends=[_row("TLY-2026-0010"), _row("TLY-2026-0011")],
    )

    assert ws.batch_update_calls == []
    assert len(ws.append_rows_calls) == 1
    call = ws.append_rows_calls[0]
    assert call.values == [_row("TLY-2026-0010"), _row("TLY-2026-0011")]
    assert call.kwargs.get("value_input_option") == "USER_ENTERED"


def test_apply_changes_updates_y_appends_se_llaman_ambos():
    ws = _FakeWorksheet()
    client, _ = _make_client(factory_result=ws)

    client.apply_changes(
        ws,
        updates=[(2, _row("TLY-2026-0001"))],
        appends=[_row("TLY-2026-0010")],
    )

    assert len(ws.batch_update_calls) == 1
    assert len(ws.append_rows_calls) == 1


def test_apply_changes_batch_update_lanza_se_envuelve():
    ws = _FakeWorksheet(batch_update_error=RuntimeError("quota"))
    client, _ = _make_client(factory_result=ws)

    with pytest.raises(SheetsClientError) as exc_info:
        client.apply_changes(
            ws, updates=[(2, _row("TLY-2026-0001"))], appends=[]
        )

    assert "batch_update fallo" in str(exc_info.value)


def test_apply_changes_append_rows_lanza_se_envuelve():
    ws = _FakeWorksheet(append_rows_error=RuntimeError("quota"))
    client, _ = _make_client(factory_result=ws)

    with pytest.raises(SheetsClientError) as exc_info:
        client.apply_changes(
            ws, updates=[], appends=[_row("TLY-2026-0010")]
        )

    assert "append_rows fallo" in str(exc_info.value)


def test_apply_changes_si_batch_update_lanza_no_intenta_appends():
    # Si la parte de updates falla, no queremos meter appends a medias.
    # El proximo ciclo reintenta todo desde cero.
    ws = _FakeWorksheet(batch_update_error=RuntimeError("quota"))
    client, _ = _make_client(factory_result=ws)

    with pytest.raises(SheetsClientError):
        client.apply_changes(
            ws,
            updates=[(2, _row("TLY-2026-0001"))],
            appends=[_row("TLY-2026-0010")],
        )

    # Append no se intento.
    assert ws.append_rows_calls == []
