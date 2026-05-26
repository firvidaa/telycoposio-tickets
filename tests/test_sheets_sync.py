"""Tests del orquestador de sync SQLite -> Sheets (``app.services.sheets_sync``).

Estrategia:

- SQLite real en ``tmp_path`` (convencion del proyecto, ver
  ``test_email_poller.py``). Inserciones directas con SQL usando
  ``to_db_row`` para no acoplar a ``ticket_service``.

- ``SheetsClient`` doblado por ``_FakeSheetsClient`` con la misma
  superficie publica (``read_state`` + ``apply_changes``). Hooks para
  inyectar errores en cualquiera de los dos metodos.

- Reloj congelado: el ``now_fn`` que se inyecta a ``SheetsSync`` devuelve
  un instante fijo. Asi podemos verificar exactamente el valor de
  ``synced_to_sheets_at`` que se escribe en BD.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.db.sqlite import get_connection, init_schema
from app.models.ticket import (
    TICKET_COLUMNS,
    Ticket,
    TicketChannel,
    TicketStatus,
    from_db_row,
    to_db_row,
)
from app.services.sheets_client import SheetsClient, SheetsClientError
from app.services.sheets_sync import SheetsSync


# ---------------------------------------------------------------------------
# Fixtures y helpers
# ---------------------------------------------------------------------------


_FROZEN_NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)


def _frozen_now() -> datetime:
    return _FROZEN_NOW


def _make_settings(db_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        APP_SECRET_KEY="x" * 32,
        ANTHROPIC_API_KEY="sk-ant-test",
        EMAIL_ADDRESS="bot@example.com",
        EMAIL_APP_PASSWORD="abcdefghijklmnop",
        SQLITE_PATH=str(db_path),
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "test.db"
    with get_connection(str(p)) as conn:
        init_schema(conn)
    return p


@pytest.fixture
def db_conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    with get_connection(str(db_path)) as conn:
        yield conn


def _make_ticket(
    *,
    tid: str,
    created_at: datetime | None = None,
    last_updated_at: datetime | None = None,
    synced_to_sheets_at: datetime | None = None,
    subject: str = "Hola",
) -> Ticket:
    """Construye un Ticket con defaults razonables para tests de sync."""
    created = created_at or _FROZEN_NOW - timedelta(hours=1)
    updated = last_updated_at or created
    return Ticket(
        id=tid,
        created_at=created,
        channel=TicketChannel.EMAIL,
        from_email="cliente@example.com",
        subject=subject,
        body="cuerpo",
        status=TicketStatus.NEW,
        last_updated_at=updated,
        synced_to_sheets_at=synced_to_sheets_at,
    )


def _insert(conn: sqlite3.Connection, ticket: Ticket) -> None:
    row = to_db_row(ticket)
    placeholders = ",".join(["?"] * len(TICKET_COLUMNS))
    columns = ",".join(TICKET_COLUMNS)
    conn.execute(
        f"INSERT INTO tickets ({columns}) VALUES ({placeholders})",
        [row[c] for c in TICKET_COLUMNS],
    )


def _load(conn: sqlite3.Connection, tid: str) -> Ticket:
    row = conn.execute("SELECT * FROM tickets WHERE id = ?", (tid,)).fetchone()
    return from_db_row(row)


# ---------------------------------------------------------------------------
# Doble del SheetsClient
# ---------------------------------------------------------------------------


class _FakeSheetsClient:
    """Doble de ``SheetsClient`` con la misma superficie publica.

    Hereda nominalmente de ``SheetsClient`` para satisfacer el type-hint
    del constructor de ``SheetsSync``, pero no llama a ``super().__init__``
    para no exigir credenciales reales.
    """

    def __init__(
        self,
        *,
        existing_id_to_row: dict[str, int] | None = None,
        read_state_error: Exception | None = None,
        apply_changes_error: Exception | None = None,
    ) -> None:
        self._existing = dict(existing_id_to_row or {})
        self._read_state_error = read_state_error
        self._apply_changes_error = apply_changes_error

        self.read_state_calls = 0
        self.apply_changes_calls: list[
            tuple[list[tuple[int, list[str]]], list[list[str]]]
        ] = []
        # Un "worksheet" dummy: solo nos sirve como sentinel para verificar
        # que SheetsSync pasa el mismo objeto que recibio.
        self._ws_sentinel = object()

    def read_state(self) -> tuple[Any, dict[str, int]]:
        self.read_state_calls += 1
        if self._read_state_error is not None:
            raise self._read_state_error
        return self._ws_sentinel, dict(self._existing)

    def apply_changes(
        self,
        ws: Any,
        *,
        updates: list[tuple[int, list[str]]],
        appends: list[list[str]],
    ) -> None:
        assert ws is self._ws_sentinel, (
            "SheetsSync debe pasar el ws devuelto por read_state"
        )
        self.apply_changes_calls.append((updates, appends))
        if self._apply_changes_error is not None:
            raise self._apply_changes_error


def _make_sync(
    db_path: Path,
    client: _FakeSheetsClient,
) -> SheetsSync:
    return SheetsSync(
        client=client,  # type: ignore[arg-type]
        settings=_make_settings(db_path),
        connect_fn=get_connection,
        now_fn=_frozen_now,
    )


# ---------------------------------------------------------------------------
# Flujo basico
# ---------------------------------------------------------------------------


def test_bd_vacia_no_llama_a_apply_changes(db_path: Path):
    client = _FakeSheetsClient()
    sync = _make_sync(db_path, client)

    subidos = sync.run_once()

    assert subidos == 0
    assert client.read_state_calls == 1
    assert client.apply_changes_calls == []


def test_ticket_nuevo_va_a_appends_y_marca_synced_to_sheets_at(
    db_path: Path, db_conn: sqlite3.Connection
):
    _insert(db_conn, _make_ticket(tid="TLY-2026-0001"))
    client = _FakeSheetsClient()  # hoja vacia
    sync = _make_sync(db_path, client)

    subidos = sync.run_once()

    assert subidos == 1
    assert len(client.apply_changes_calls) == 1
    updates, appends = client.apply_changes_calls[0]
    assert updates == []
    assert len(appends) == 1
    # Primera celda de la fila = ID.
    assert appends[0][0] == "TLY-2026-0001"

    # BD: synced_to_sheets_at se escribio con el reloj congelado.
    persisted = _load(db_conn, "TLY-2026-0001")
    assert persisted.synced_to_sheets_at == _FROZEN_NOW


def test_ticket_existente_modificado_va_a_updates(
    db_path: Path, db_conn: sqlite3.Connection
):
    # Ticket ya sincronizado, pero modificado despues.
    synced_at = _FROZEN_NOW - timedelta(hours=2)
    updated_at = _FROZEN_NOW - timedelta(minutes=10)
    _insert(
        db_conn,
        _make_ticket(
            tid="TLY-2026-0001",
            synced_to_sheets_at=synced_at,
            last_updated_at=updated_at,
        ),
    )
    # La hoja ya tiene este ID en la fila 2.
    client = _FakeSheetsClient(existing_id_to_row={"TLY-2026-0001": 2})
    sync = _make_sync(db_path, client)

    subidos = sync.run_once()

    assert subidos == 1
    updates, appends = client.apply_changes_calls[0]
    assert appends == []
    assert len(updates) == 1
    row_idx, row_values = updates[0]
    assert row_idx == 2
    assert row_values[0] == "TLY-2026-0001"


def test_mezcla_nuevos_y_modificados_en_una_sola_llamada(
    db_path: Path, db_conn: sqlite3.Connection
):
    # Uno nuevo, uno modificado.
    _insert(db_conn, _make_ticket(tid="TLY-2026-0001"))  # nuevo
    _insert(
        db_conn,
        _make_ticket(
            tid="TLY-2026-0002",
            synced_to_sheets_at=_FROZEN_NOW - timedelta(hours=2),
            last_updated_at=_FROZEN_NOW - timedelta(minutes=5),
        ),
    )
    client = _FakeSheetsClient(existing_id_to_row={"TLY-2026-0002": 7})
    sync = _make_sync(db_path, client)

    subidos = sync.run_once()

    assert subidos == 2
    assert len(client.apply_changes_calls) == 1
    updates, appends = client.apply_changes_calls[0]
    # El existente va a updates, el nuevo a appends.
    assert [r for r, _ in updates] == [7]
    assert len(appends) == 1
    assert appends[0][0] == "TLY-2026-0001"


def test_ya_sincronizados_sin_cambios_no_se_vuelven_a_subir(
    db_path: Path, db_conn: sqlite3.Connection
):
    synced_at = _FROZEN_NOW - timedelta(hours=1)
    # last_updated_at == synced_at (no es mayor): no se considera pendiente.
    _insert(
        db_conn,
        _make_ticket(
            tid="TLY-2026-0001",
            synced_to_sheets_at=synced_at,
            last_updated_at=synced_at,
        ),
    )
    client = _FakeSheetsClient(existing_id_to_row={"TLY-2026-0001": 2})
    sync = _make_sync(db_path, client)

    subidos = sync.run_once()

    assert subidos == 0
    assert client.apply_changes_calls == []


# ---------------------------------------------------------------------------
# Idempotencia y no-bucle-infinito
# ---------------------------------------------------------------------------


def test_segundo_run_once_no_resincroniza_si_no_hubo_cambios(
    db_path: Path, db_conn: sqlite3.Connection
):
    _insert(db_conn, _make_ticket(tid="TLY-2026-0001"))
    client = _FakeSheetsClient()
    sync = _make_sync(db_path, client)

    # Primera vuelta: sube el ticket.
    assert sync.run_once() == 1
    # Segunda vuelta inmediata: no debe subir nada.
    # Importante: el fake "hoja" no aprende del append automaticamente;
    # como la BD ya marca synced_to_sheets_at, el SELECT no devuelve
    # nada y apply_changes no se invoca. Esa es la propiedad clave.
    assert sync.run_once() == 0
    assert len(client.apply_changes_calls) == 1


def test_sync_no_actualiza_last_updated_at_evita_bucle_infinito(
    db_path: Path, db_conn: sqlite3.Connection
):
    """Regresion: si ``_mark_synced`` tocara ``last_updated_at``, el
    siguiente ciclo veria el ticket como modificado y entrariamos en
    bucle. Verificamos que el timestamp original NO cambia tras el sync.
    """
    original_updated = _FROZEN_NOW - timedelta(hours=3)
    _insert(
        db_conn,
        _make_ticket(
            tid="TLY-2026-0001",
            last_updated_at=original_updated,
        ),
    )
    client = _FakeSheetsClient()
    sync = _make_sync(db_path, client)

    sync.run_once()

    persisted = _load(db_conn, "TLY-2026-0001")
    assert persisted.last_updated_at == original_updated
    assert persisted.synced_to_sheets_at == _FROZEN_NOW


# ---------------------------------------------------------------------------
# Resiliencia
# ---------------------------------------------------------------------------


def test_read_state_lanza_sheets_client_error_devuelve_cero_y_bd_intacta(
    db_path: Path, db_conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
):
    _insert(db_conn, _make_ticket(tid="TLY-2026-0001"))
    client = _FakeSheetsClient(read_state_error=SheetsClientError("api caida"))
    sync = _make_sync(db_path, client)

    with caplog.at_level(logging.WARNING, logger="app.services.sheets_sync"):
        subidos = sync.run_once()

    assert subidos == 0
    # BD intacta: nadie marco synced.
    persisted = _load(db_conn, "TLY-2026-0001")
    assert persisted.synced_to_sheets_at is None
    # Log de WARNING con el mensaje.
    assert any("api caida" in rec.message for rec in caplog.records)


def test_apply_changes_lanza_sheets_client_error_no_marca_synced(
    db_path: Path, db_conn: sqlite3.Connection
):
    _insert(db_conn, _make_ticket(tid="TLY-2026-0001"))
    client = _FakeSheetsClient(
        apply_changes_error=SheetsClientError("batch_update fallo")
    )
    sync = _make_sync(db_path, client)

    subidos = sync.run_once()

    assert subidos == 0
    # apply_changes se intento (no es un fallo previo).
    assert len(client.apply_changes_calls) == 1
    # Pero el ticket sigue sin marcar como sincronizado.
    persisted = _load(db_conn, "TLY-2026-0001")
    assert persisted.synced_to_sheets_at is None


def test_excepcion_inesperada_se_captura_y_loguea_exception(
    db_path: Path, db_conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
):
    """Defensa: cualquier excepcion fuera de ``SheetsClientError`` debe
    capturarse para no tirar el scheduler. Logueamos con ``exception``.
    """
    _insert(db_conn, _make_ticket(tid="TLY-2026-0001"))
    client = _FakeSheetsClient(
        apply_changes_error=RuntimeError("algo raro paso")
    )
    sync = _make_sync(db_path, client)

    with caplog.at_level(logging.ERROR, logger="app.services.sheets_sync"):
        subidos = sync.run_once()

    assert subidos == 0
    # BD intacta.
    persisted = _load(db_conn, "TLY-2026-0001")
    assert persisted.synced_to_sheets_at is None
    # Log de error.
    assert any("RuntimeError" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Orden y limite
# ---------------------------------------------------------------------------


def test_appends_van_en_orden_cronologico_ascendente(
    db_path: Path, db_conn: sqlite3.Connection
):
    # Insertamos en orden invertido para verificar que el ORDER BY ASC
    # los devuelve cronologicos pese al orden de insercion.
    _insert(
        db_conn,
        _make_ticket(
            tid="TLY-2026-0003",
            created_at=_FROZEN_NOW - timedelta(minutes=10),
        ),
    )
    _insert(
        db_conn,
        _make_ticket(
            tid="TLY-2026-0001",
            created_at=_FROZEN_NOW - timedelta(minutes=30),
        ),
    )
    _insert(
        db_conn,
        _make_ticket(
            tid="TLY-2026-0002",
            created_at=_FROZEN_NOW - timedelta(minutes=20),
        ),
    )

    client = _FakeSheetsClient()
    sync = _make_sync(db_path, client)
    sync.run_once()

    _, appends = client.apply_changes_calls[0]
    ids = [row[0] for row in appends]
    assert ids == ["TLY-2026-0001", "TLY-2026-0002", "TLY-2026-0003"]


def test_max_results_limita_el_numero_de_tickets_por_iteracion(
    db_path: Path, db_conn: sqlite3.Connection
):
    for i in range(5):
        _insert(
            db_conn,
            _make_ticket(
                tid=f"TLY-2026-000{i + 1}",
                created_at=_FROZEN_NOW - timedelta(minutes=60 - i),
            ),
        )
    client = _FakeSheetsClient()
    sync = _make_sync(db_path, client)

    subidos = sync.run_once(max_results=3)

    assert subidos == 3
    _, appends = client.apply_changes_calls[0]
    assert len(appends) == 3
    # En el siguiente ciclo deberian quedar 2 pendientes.
    subidos_2 = sync.run_once(max_results=3)
    assert subidos_2 == 2
