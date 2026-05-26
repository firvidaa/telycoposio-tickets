"""Tests del modelo de datos y la capa SQLite.

Usan una BD ``:memory:`` para no tocar disco.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from app.db.sqlite import get_connection, init_schema
from app.models import ticket as ticket_module
from app.models import user as user_module
from app.models.ticket import (
    Attachment,
    Ticket,
    TicketCategory,
    TicketChannel,
    TicketStatus,
)
from app.models.user import User


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn():
    """Conexion a una BD en memoria con el esquema ya aplicado."""
    with get_connection(":memory:") as c:
        init_schema(c)
        yield c


# ---------------------------------------------------------------------------
# Esquema
# ---------------------------------------------------------------------------


def test_init_schema_crea_tablas_esperadas(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = [r["name"] for r in rows]
    # ticket_replies (Paso 10) + tickets + users.
    assert tables == ["ticket_replies", "tickets", "users"]


def test_init_schema_crea_indice_ticket_replies(conn: sqlite3.Connection) -> None:
    """El indice compuesto (ticket_id, sent_at) cubre la consulta del
    historial: ``WHERE ticket_id = ? ORDER BY sent_at ASC``.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name = 'idx_ticket_replies_ticket_id'"
    ).fetchone()
    assert row is not None


def test_ticket_replies_fk_cascade_borra_replies_con_ticket(
    conn: sqlite3.Connection,
) -> None:
    """Borrar un ticket arrastra sus respuestas (ON DELETE CASCADE)."""
    # Crear ticket y usuario minimos.
    conn.execute(
        "INSERT INTO tickets (id, created_at, channel, subject, body, "
        "status, last_updated_at) VALUES "
        "('TLY-2026-0001', '2026-05-30T09:00:00+00:00', 'email', 's', 'b', 'NEW', "
        "'2026-05-30T09:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO users (username, password_hash, display_name, role, created_at) "
        "VALUES ('u', 'h', 'U', 'user', '2026-01-01T00:00:00+00:00')"
    )
    user_id = conn.execute("SELECT id FROM users").fetchone()["id"]
    conn.execute(
        "INSERT INTO ticket_replies (ticket_id, sent_at, user_id, body, message_id) "
        "VALUES ('TLY-2026-0001', '2026-05-30T10:00:00+00:00', ?, 'x', '<m@x>')",
        (user_id,),
    )
    assert conn.execute("SELECT COUNT(*) AS n FROM ticket_replies").fetchone()["n"] == 1

    conn.execute("DELETE FROM tickets WHERE id = 'TLY-2026-0001'")
    assert conn.execute("SELECT COUNT(*) AS n FROM ticket_replies").fetchone()["n"] == 0


def test_init_schema_es_idempotente(conn: sqlite3.Connection) -> None:
    # Ejecutarlo otra vez no debe explotar.
    init_schema(conn)


# ---------------------------------------------------------------------------
# Roundtrip de Ticket
# ---------------------------------------------------------------------------


def _ticket_completo() -> Ticket:
    """Ticket con todos los campos rellenos para forzar roundtrip total."""
    return Ticket(
        id="TLY-2026-0001",
        created_at=datetime(2026, 5, 8, 9, 30, 0, tzinfo=timezone.utc),
        channel=TicketChannel.EMAIL,
        from_name="Cliente Ejemplo",
        from_email="cliente@example.com",
        from_phone=None,
        subject="No me funciona el telefono",
        body="Buenos dias, mi telefono IP no da tono...",
        status=TicketStatus.NEW,
        category=TicketCategory.SOPORTE,
        category_confidence=0.92,
        category_reasoning="Menciona telefono IP -> SOPORTE.",
        category_manual_override=False,
        needs_review=False,
        raw_message_id="<abc123@mail.gmail.com>",
        attachments=[Attachment(name="captura.png", url="https://x/y.png")],
        client_notified_at=datetime(2026, 5, 8, 9, 30, 5, tzinfo=timezone.utc),
        last_updated_at=datetime(2026, 5, 8, 9, 30, 5, tzinfo=timezone.utc),
        synced_to_sheets_at=None,
    )


def _insert_ticket(conn: sqlite3.Connection, ticket: Ticket) -> None:
    row = ticket_module.to_db_row(ticket)
    cols = ticket_module.TICKET_COLUMNS
    placeholders = ", ".join(f":{c}" for c in cols)
    conn.execute(
        f"INSERT INTO tickets ({', '.join(cols)}) VALUES ({placeholders})",
        row,
    )


def test_ticket_roundtrip_completo(conn: sqlite3.Connection) -> None:
    original = _ticket_completo()
    _insert_ticket(conn, original)

    row = conn.execute(
        "SELECT * FROM tickets WHERE id = ?", (original.id,)
    ).fetchone()
    assert row is not None

    leido = ticket_module.from_db_row(row)
    assert leido == original


def test_ticket_categoria_null_y_needs_review_true(conn: sqlite3.Connection) -> None:
    """Cuando la IA falla: category=NULL, confidence=NULL, needs_review=True."""
    t = Ticket(
        id="TLY-2026-0002",
        created_at=datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc),
        channel=TicketChannel.EMAIL,
        subject="Asunto X",
        body="Cuerpo X",
        category=None,
        category_confidence=None,
        category_reasoning=None,
        needs_review=True,
        last_updated_at=datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc),
    )
    _insert_ticket(conn, t)

    row = conn.execute(
        "SELECT * FROM tickets WHERE id = ?", (t.id,)
    ).fetchone()
    leido = ticket_module.from_db_row(row)
    assert leido.category is None
    assert leido.category_confidence is None
    assert leido.needs_review is True


def test_ticket_id_duplicado_falla(conn: sqlite3.Connection) -> None:
    t = _ticket_completo()
    _insert_ticket(conn, t)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_ticket(conn, t)


def test_ticket_raw_message_id_unico_pero_acepta_varios_null(
    conn: sqlite3.Connection,
) -> None:
    base = _ticket_completo()
    _insert_ticket(conn, base)

    duplicado = base.model_copy(update={"id": "TLY-2026-0099"})
    with pytest.raises(sqlite3.IntegrityError):
        _insert_ticket(conn, duplicado)

    # Dos tickets sin raw_message_id (canal voicemail, p.ej.) deben coexistir.
    sin_id_1 = base.model_copy(update={"id": "TLY-2026-0100", "raw_message_id": None})
    sin_id_2 = base.model_copy(update={"id": "TLY-2026-0101", "raw_message_id": None})
    _insert_ticket(conn, sin_id_1)
    _insert_ticket(conn, sin_id_2)


def test_ticket_check_constraint_status_invalido(conn: sqlite3.Connection) -> None:
    """Si alguien escribe SQL crudo con un status invalido, la BD lo rechaza."""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO tickets (id, created_at, channel, subject, body, status, "
            "last_updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "TLY-2026-0500",
                "2026-05-08T00:00:00+00:00",
                "email",
                "x",
                "x",
                "INVENTADO",
                "2026-05-08T00:00:00+00:00",
            ),
        )


# ---------------------------------------------------------------------------
# Roundtrip de User
# ---------------------------------------------------------------------------


def _insert_user(conn: sqlite3.Connection, user: User) -> int:
    row = user_module.to_db_row(user)
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, display_name, email, role, created_at) "
        "VALUES (:username, :password_hash, :display_name, :email, :role, :created_at)",
        row,
    )
    return cur.lastrowid  # type: ignore[return-value]


def test_user_roundtrip(conn: sqlite3.Connection) -> None:
    original = User(
        username="alfredo",
        password_hash="$2b$12$fakehashfakehashfakehashfakehash",
        display_name="Alfredo",
        email="alfredo@example.com",
        role="admin",
        created_at=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
    )
    new_id = _insert_user(conn, original)

    row = conn.execute("SELECT * FROM users WHERE id = ?", (new_id,)).fetchone()
    leido = user_module.from_db_row(row)

    assert leido.id == new_id
    # comparamos campo a campo ignorando id (que en el original era None).
    assert leido.username == original.username
    assert leido.password_hash == original.password_hash
    assert leido.display_name == original.display_name
    assert leido.email == original.email
    assert leido.role == original.role
    assert leido.created_at == original.created_at


def test_user_username_unico(conn: sqlite3.Connection) -> None:
    u = User(
        username="alfredo",
        password_hash="x",
        display_name="A",
        created_at=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
    )
    _insert_user(conn, u)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_user(conn, u)


def test_user_role_invalido_lo_rechaza_la_bd(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO users (username, password_hash, display_name, role, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("x", "x", "X", "superuser", "2026-05-08T00:00:00+00:00"),
        )
