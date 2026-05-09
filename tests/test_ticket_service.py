"""Tests del servicio de tickets (``app.services.ticket_service``).

BD ``:memory:`` con esquema cargado, sin tocar disco.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.db.sqlite import get_connection, init_schema
from app.models.ticket import (
    TICKET_COLUMNS,
    Attachment,
    Ticket,
    TicketCategory,
    TicketChannel,
    TicketStatus,
    to_db_row,
)
from app.services.ticket_service import (
    MAX_TICKETS_PER_YEAR,
    NEEDS_REVIEW_THRESHOLD,
    TicketIdOverflowError,
    create_ticket,
    get_ticket,
)

MADRID = ZoneInfo("Europe/Madrid")


# ---------------------------------------------------------------------------
# Fixtures y helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    with get_connection(":memory:") as c:
        init_schema(c)
        yield c


def _make_ticket(conn: sqlite3.Connection, **overrides: Any) -> Ticket:
    """Helper: crea un ticket con valores por defecto sensatos."""
    params: dict[str, Any] = dict(
        conn=conn,
        channel=TicketChannel.EMAIL,
        subject="Asunto",
        body="Cuerpo",
        from_email="cliente@example.com",
        category=TicketCategory.SOPORTE,
        category_confidence=0.95,
        now=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
    )
    params.update(overrides)
    return create_ticket(**params)


def _insert_raw(conn: sqlite3.Connection, ticket: Ticket) -> None:
    """Inserta un ticket directamente sin pasar por ``create_ticket``.

    Util para preparar estado (ejemplo: forzar ``TLY-2026-9999`` para luego
    probar el overflow sin tener que crear 9999 tickets reales).
    """
    placeholders = ", ".join(f":{c}" for c in TICKET_COLUMNS)
    conn.execute(
        f"INSERT INTO tickets ({', '.join(TICKET_COLUMNS)}) VALUES ({placeholders})",
        to_db_row(ticket),
    )


# ---------------------------------------------------------------------------
# Generacion de IDs
# ---------------------------------------------------------------------------


def test_id_rollover_normal(conn: sqlite3.Connection) -> None:
    t1 = _make_ticket(conn, raw_message_id="msg-1")
    t2 = _make_ticket(conn, raw_message_id="msg-2")
    t3 = _make_ticket(conn, raw_message_id="msg-3")
    assert t1.id == "TLY-2026-0001"
    assert t2.id == "TLY-2026-0002"
    assert t3.id == "TLY-2026-0003"


def test_id_reinicia_en_cambio_de_anyo_madrid(conn: sqlite3.Connection) -> None:
    """22:00 del 31/dic en Madrid → TLY-2026; 00:05 del 1/ene en Madrid → TLY-2027-0001."""
    t_dic = _make_ticket(
        conn,
        now=datetime(2026, 12, 31, 22, 0, tzinfo=MADRID),
        raw_message_id="msg-y26",
    )
    t_ene = _make_ticket(
        conn,
        now=datetime(2027, 1, 1, 0, 5, tzinfo=MADRID),
        raw_message_id="msg-y27",
    )
    assert t_dic.id == "TLY-2026-0001"
    assert t_ene.id == "TLY-2027-0001"


def test_anyo_se_calcula_en_madrid_no_en_utc(conn: sqlite3.Connection) -> None:
    """Edge case: 23:30 UTC del 31/dic/2026 = 00:30 Madrid del 1/ene/2027.

    El anyo del ID debe ser 2027 (Madrid), no 2026 (UTC).
    """
    t = _make_ticket(
        conn,
        now=datetime(2026, 12, 31, 23, 30, tzinfo=timezone.utc),
        raw_message_id="msg-edge",
    )
    assert t.id.startswith("TLY-2027-"), f"Se esperaba TLY-2027-*, llegó {t.id}"


def test_overflow_levanta_excepcion_especifica(conn: sqlite3.Connection) -> None:
    """Si ya existe TLY-2026-9999, el siguiente intento debe levantar TicketIdOverflowError."""
    last = Ticket(
        id=f"TLY-2026-{MAX_TICKETS_PER_YEAR:04d}",
        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        channel=TicketChannel.EMAIL,
        subject="x",
        body="x",
        last_updated_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    _insert_raw(conn, last)

    with pytest.raises(TicketIdOverflowError):
        _make_ticket(conn, raw_message_id="overflow")


# ---------------------------------------------------------------------------
# Dedup por raw_message_id
# ---------------------------------------------------------------------------


def test_dedup_devuelve_existente_sin_crear_nuevo(conn: sqlite3.Connection) -> None:
    t1 = _make_ticket(conn, raw_message_id="duplicado")
    t2 = _make_ticket(conn, raw_message_id="duplicado", subject="Otro asunto")
    # Es el mismo ticket, no se ha creado uno nuevo.
    assert t2.id == t1.id
    assert t2.subject == "Asunto"  # mantiene el original, no sobrescribe

    count = conn.execute("SELECT COUNT(*) AS c FROM tickets").fetchone()["c"]
    assert count == 1


def test_dedup_solo_aplica_si_raw_message_id_no_es_none(
    conn: sqlite3.Connection,
) -> None:
    """Tickets sin raw_message_id (canal voicemail) NO se deduplican entre si."""
    t1 = _make_ticket(conn, raw_message_id=None, subject="A")
    t2 = _make_ticket(conn, raw_message_id=None, subject="B")
    assert t1.id != t2.id


# ---------------------------------------------------------------------------
# needs_review automatico
# ---------------------------------------------------------------------------


def test_needs_review_categoria_none(conn: sqlite3.Connection) -> None:
    t = _make_ticket(
        conn, category=None, category_confidence=None, raw_message_id="r1"
    )
    assert t.needs_review is True


def test_needs_review_confianza_none_pero_categoria_si(
    conn: sqlite3.Connection,
) -> None:
    """Si tenemos categoria pero no confianza, mejor revisar."""
    t = _make_ticket(
        conn,
        category=TicketCategory.SOPORTE,
        category_confidence=None,
        raw_message_id="r1b",
    )
    assert t.needs_review is True


def test_needs_review_confianza_baja(conn: sqlite3.Connection) -> None:
    t = _make_ticket(
        conn,
        category=TicketCategory.SOPORTE,
        category_confidence=0.5,
        raw_message_id="r2",
    )
    assert t.needs_review is True


def test_needs_review_confianza_alta(conn: sqlite3.Connection) -> None:
    t = _make_ticket(
        conn,
        category=TicketCategory.SOPORTE,
        category_confidence=0.95,
        raw_message_id="r3",
    )
    assert t.needs_review is False


def test_needs_review_threshold_exacto_no_marca(conn: sqlite3.Connection) -> None:
    """El SPEC dice 'menor que 0.7'. En 0.7 exacto no se marca para revision."""
    t = _make_ticket(
        conn,
        category=TicketCategory.SOPORTE,
        category_confidence=NEEDS_REVIEW_THRESHOLD,
        raw_message_id="r4",
    )
    assert t.needs_review is False


# ---------------------------------------------------------------------------
# Roundtrip de attachments
# ---------------------------------------------------------------------------


def test_persistencia_attachments_roundtrip(conn: sqlite3.Connection) -> None:
    adj = [
        Attachment(name="factura.pdf", url="https://x/factura.pdf"),
        Attachment(name="captura.png", url="https://x/captura.png"),
    ]
    creado = _make_ticket(conn, attachments=adj, raw_message_id="att-1")

    leido = get_ticket(conn, creado.id)
    assert leido is not None
    assert leido.attachments == adj


# ---------------------------------------------------------------------------
# Defaults del ticket recien creado
# ---------------------------------------------------------------------------


def test_status_inicial_es_NEW(conn: sqlite3.Connection) -> None:
    t = _make_ticket(conn, raw_message_id="x")
    assert t.status is TicketStatus.NEW


def test_synced_to_sheets_at_inicial_es_none(conn: sqlite3.Connection) -> None:
    t = _make_ticket(conn, raw_message_id="x")
    assert t.synced_to_sheets_at is None


def test_client_notified_at_inicial_es_none(conn: sqlite3.Connection) -> None:
    t = _make_ticket(conn, raw_message_id="x")
    assert t.client_notified_at is None


def test_category_manual_override_inicial_es_false(conn: sqlite3.Connection) -> None:
    t = _make_ticket(conn, raw_message_id="x")
    assert t.category_manual_override is False


# ---------------------------------------------------------------------------
# get_ticket
# ---------------------------------------------------------------------------


def test_get_ticket_no_existe_devuelve_none(conn: sqlite3.Connection) -> None:
    assert get_ticket(conn, "TLY-2099-9999") is None
