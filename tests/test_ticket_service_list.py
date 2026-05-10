"""Tests de ``list_tickets`` y ``count_tickets`` en ``app.services.ticket_service``.

BD ``:memory:`` con esquema cargado. Los tickets se insertan a mano (sin pasar
por ``create_ticket``) para controlar fechas y campos exactos.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.db.sqlite import get_connection, init_schema
from app.models.ticket import (
    TICKET_COLUMNS,
    Ticket,
    TicketCategory,
    TicketChannel,
    TicketStatus,
    to_db_row,
)
from app.services.ticket_service import (
    count_tickets,
    list_tickets,
)


# ---------------------------------------------------------------------------
# Fixtures y helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    with get_connection(":memory:") as c:
        init_schema(c)
        yield c


def _insert(conn: sqlite3.Connection, ticket: Ticket) -> None:
    row = to_db_row(ticket)
    placeholders = ", ".join(f":{c}" for c in TICKET_COLUMNS)
    conn.execute(
        f"INSERT INTO tickets ({', '.join(TICKET_COLUMNS)}) VALUES ({placeholders})",
        row,
    )


def _make(
    conn: sqlite3.Connection,
    *,
    n: int,
    minute_offset: int = 0,
    **fields: Any,
) -> str:
    """Inserta un ticket con valores por defecto razonables y devuelve su id.

    ``minute_offset`` permite simular tickets creados en momentos distintos
    para que el orden por ``created_at DESC`` sea verificable.
    """
    base = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    created = base + timedelta(minutes=minute_offset)
    defaults: dict[str, Any] = dict(
        id=f"TLY-2026-{n:04d}",
        created_at=created,
        channel=TicketChannel.EMAIL,
        subject=f"Asunto {n}",
        body="cuerpo",
        status=TicketStatus.NEW,
        category=TicketCategory.SOPORTE,
        category_confidence=0.9,
        needs_review=False,
        last_updated_at=created,
    )
    defaults.update(fields)
    ticket = Ticket(**defaults)
    _insert(conn, ticket)
    return ticket.id


# ---------------------------------------------------------------------------
# Vacio / orden / paginacion
# ---------------------------------------------------------------------------


def test_list_vacio(conn: sqlite3.Connection) -> None:
    assert list_tickets(conn) == []
    assert count_tickets(conn) == 0


def test_list_ordena_por_created_at_descendente(conn: sqlite3.Connection) -> None:
    _make(conn, n=1, minute_offset=0)   # mas viejo
    _make(conn, n=2, minute_offset=10)
    _make(conn, n=3, minute_offset=20)  # mas reciente

    ids = [t.id for t in list_tickets(conn)]
    assert ids == ["TLY-2026-0003", "TLY-2026-0002", "TLY-2026-0001"]


def test_list_limit_y_offset(conn: sqlite3.Connection) -> None:
    for i in range(1, 6):
        _make(conn, n=i, minute_offset=i)

    primera = list_tickets(conn, limit=2, offset=0)
    segunda = list_tickets(conn, limit=2, offset=2)
    tercera = list_tickets(conn, limit=2, offset=4)

    assert [t.id for t in primera] == ["TLY-2026-0005", "TLY-2026-0004"]
    assert [t.id for t in segunda] == ["TLY-2026-0003", "TLY-2026-0002"]
    assert [t.id for t in tercera] == ["TLY-2026-0001"]


# ---------------------------------------------------------------------------
# Filtros
# ---------------------------------------------------------------------------


def test_filtro_status_in(conn: sqlite3.Connection) -> None:
    _make(conn, n=1, status=TicketStatus.NEW)
    _make(conn, n=2, status=TicketStatus.IN_PROGRESS)
    _make(conn, n=3, status=TicketStatus.WAITING)
    _make(conn, n=4, status=TicketStatus.CLOSED)

    activos = list_tickets(
        conn,
        statuses=[TicketStatus.NEW, TicketStatus.IN_PROGRESS, TicketStatus.WAITING],
    )
    ids = sorted(t.id for t in activos)
    assert ids == ["TLY-2026-0001", "TLY-2026-0002", "TLY-2026-0003"]


def test_filtro_status_unico(conn: sqlite3.Connection) -> None:
    _make(conn, n=1, status=TicketStatus.NEW)
    _make(conn, n=2, status=TicketStatus.CLOSED)

    cerrados = list_tickets(conn, statuses=[TicketStatus.CLOSED])
    assert [t.id for t in cerrados] == ["TLY-2026-0002"]


def test_statuses_none_no_filtra(conn: sqlite3.Connection) -> None:
    _make(conn, n=1, status=TicketStatus.NEW)
    _make(conn, n=2, status=TicketStatus.CLOSED)
    assert len(list_tickets(conn, statuses=None)) == 2


def test_statuses_lista_vacia_devuelve_vacio(conn: sqlite3.Connection) -> None:
    """``statuses=[]`` es 'ningun status coincide' — el resultado es vacio."""
    _make(conn, n=1, status=TicketStatus.NEW)
    _make(conn, n=2, status=TicketStatus.CLOSED)
    assert list_tickets(conn, statuses=[]) == []
    assert count_tickets(conn, statuses=[]) == 0


def test_filtro_category(conn: sqlite3.Connection) -> None:
    _make(conn, n=1, category=TicketCategory.SOPORTE)
    _make(conn, n=2, category=TicketCategory.COMERCIAL)
    _make(conn, n=3, category=TicketCategory.ADMINISTRATIVO)

    res = list_tickets(conn, category=TicketCategory.COMERCIAL)
    assert [t.id for t in res] == ["TLY-2026-0002"]


def test_filtro_only_uncategorized(conn: sqlite3.Connection) -> None:
    _make(conn, n=1, category=TicketCategory.SOPORTE)
    _make(conn, n=2, category=None, category_confidence=None, needs_review=True)
    _make(conn, n=3, category=None, category_confidence=None, needs_review=True)

    res = list_tickets(conn, only_uncategorized=True)
    ids = sorted(t.id for t in res)
    assert ids == ["TLY-2026-0002", "TLY-2026-0003"]


def test_only_uncategorized_gana_a_category(conn: sqlite3.Connection) -> None:
    """Si se pide ``only_uncategorized``, ``category`` se ignora."""
    _make(conn, n=1, category=TicketCategory.SOPORTE)
    _make(conn, n=2, category=None, category_confidence=None, needs_review=True)
    res = list_tickets(
        conn,
        only_uncategorized=True,
        category=TicketCategory.SOPORTE,
    )
    assert [t.id for t in res] == ["TLY-2026-0002"]


def test_filtro_needs_review_true(conn: sqlite3.Connection) -> None:
    _make(conn, n=1, needs_review=False)
    _make(conn, n=2, needs_review=True)
    _make(conn, n=3, needs_review=False)

    res = list_tickets(conn, needs_review=True)
    assert [t.id for t in res] == ["TLY-2026-0002"]


def test_filtro_needs_review_false(conn: sqlite3.Connection) -> None:
    _make(conn, n=1, needs_review=False, minute_offset=0)
    _make(conn, n=2, needs_review=True, minute_offset=10)
    _make(conn, n=3, needs_review=False, minute_offset=20)

    res = list_tickets(conn, needs_review=False)
    ids = [t.id for t in res]
    # El de needs_review=True (n=2) queda fuera; orden DESC por created_at.
    assert ids == ["TLY-2026-0003", "TLY-2026-0001"]


def test_filtro_needs_review_none_no_filtra(conn: sqlite3.Connection) -> None:
    _make(conn, n=1, needs_review=False)
    _make(conn, n=2, needs_review=True)
    assert len(list_tickets(conn, needs_review=None)) == 2


def test_filtros_combinados(conn: sqlite3.Connection) -> None:
    """status=NEW + category=SOPORTE + needs_review=False."""
    # Match
    _make(
        conn, n=1,
        status=TicketStatus.NEW, category=TicketCategory.SOPORTE, needs_review=False,
    )
    # Status distinto
    _make(
        conn, n=2,
        status=TicketStatus.CLOSED, category=TicketCategory.SOPORTE, needs_review=False,
    )
    # Categoria distinta
    _make(
        conn, n=3,
        status=TicketStatus.NEW, category=TicketCategory.COMERCIAL, needs_review=False,
    )
    # needs_review True
    _make(
        conn, n=4,
        status=TicketStatus.NEW, category=TicketCategory.SOPORTE, needs_review=True,
    )

    res = list_tickets(
        conn,
        statuses=[TicketStatus.NEW],
        category=TicketCategory.SOPORTE,
        needs_review=False,
    )
    assert [t.id for t in res] == ["TLY-2026-0001"]


# ---------------------------------------------------------------------------
# count_tickets
# ---------------------------------------------------------------------------


def test_count_tickets_respeta_filtros(conn: sqlite3.Connection) -> None:
    _make(conn, n=1, status=TicketStatus.NEW)
    _make(conn, n=2, status=TicketStatus.NEW)
    _make(conn, n=3, status=TicketStatus.CLOSED)
    assert count_tickets(conn) == 3
    assert count_tickets(conn, statuses=[TicketStatus.NEW]) == 2
    assert count_tickets(conn, statuses=[TicketStatus.CLOSED]) == 1
