"""Tests del servicio de respuestas al cliente (``app.services.reply_service``).

Estrategia:

- BD SQLite ``:memory:`` con esquema cargado.
- Cliente SMTP doblado por ``_FakeEmailSender`` con la superficie minima
  (``send``). Programable: lanza excepcion si se le pide.
- Reloj congelado (`_FROZEN_NOW`).
- Helper que crea un ticket de email con todos los campos necesarios.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import pytest

from app.db.sqlite import get_connection, init_schema
from app.models.reply import Reply
from app.models.ticket import (
    TICKET_COLUMNS,
    Ticket,
    TicketChannel,
    TicketStatus,
    to_db_row,
)
from app.models.user import User
from app.services.reply_service import _compute_reply_subject, send_reply
from app.services.ticket_service import get_replies, get_ticket


_FROZEN_NOW = datetime(2026, 6, 1, 10, 30, 0, tzinfo=timezone.utc)


def _frozen_now() -> datetime:
    return _FROZEN_NOW


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    with get_connection(":memory:") as c:
        init_schema(c)
        yield c


@pytest.fixture
def user(conn: sqlite3.Connection) -> User:
    """Usuario persistido en la tabla users (necesario por la FK)."""
    conn.execute(
        "INSERT INTO users (username, password_hash, display_name, role, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            "operadora",
            "$2b$12$fakehash",
            "Operadora",
            "user",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    uid = conn.execute(
        "SELECT id FROM users WHERE username = ?", ("operadora",)
    ).fetchone()["id"]
    return User(
        id=uid,
        username="operadora",
        password_hash="$2b$12$fakehash",
        display_name="Operadora",
        role="user",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _insert_ticket(conn: sqlite3.Connection, **overrides: Any) -> Ticket:
    """Inserta un ticket de email valido para responder. Override opcional."""
    defaults: dict[str, Any] = dict(
        id="TLY-2026-0001",
        created_at=datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc),
        channel=TicketChannel.EMAIL,
        from_email="cliente@example.com",
        from_name="Cliente",
        from_phone=None,
        subject="Problema con el router",
        body="No me conecta a internet desde ayer.",
        status=TicketStatus.NEW,
        category=None,
        category_confidence=None,
        category_reasoning=None,
        category_manual_override=False,
        needs_review=True,
        raw_message_id="<original-msg@cliente.com>",
        attachments=[],
        client_notified_at=None,
        last_updated_at=datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc),
        synced_to_sheets_at=None,
    )
    defaults.update(overrides)
    ticket = Ticket(**defaults)
    placeholders = ", ".join(f":{c}" for c in TICKET_COLUMNS)
    conn.execute(
        f"INSERT INTO tickets ({', '.join(TICKET_COLUMNS)}) VALUES ({placeholders})",
        to_db_row(ticket),
    )
    return ticket


# ---------------------------------------------------------------------------
# Doble del cliente SMTP
# ---------------------------------------------------------------------------


class _FakeEmailSender:
    """Doble: registra llamadas a ``send`` y opcionalmente lanza."""

    def __init__(
        self,
        *,
        message_id: str = "<generated@example.com>",
        send_error: Exception | None = None,
    ) -> None:
        self.message_id = message_id
        self._send_error = send_error
        self.calls: list[dict[str, Any]] = []

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        in_reply_to: str | None = None,
    ) -> str:
        self.calls.append(
            {
                "to": to,
                "subject": subject,
                "body": body,
                "in_reply_to": in_reply_to,
            }
        )
        if self._send_error is not None:
            raise self._send_error
        return self.message_id


# ---------------------------------------------------------------------------
# _compute_reply_subject (helper interno)
# ---------------------------------------------------------------------------


def test_compute_subject_anyade_re_si_no_esta():
    assert _compute_reply_subject("Problema con el router") == "Re: Problema con el router"


def test_compute_subject_no_duplica_re_existente():
    assert _compute_reply_subject("Re: Problema con el router") == "Re: Problema con el router"


def test_compute_subject_no_duplica_re_mayusculas():
    # Aceptamos cualquier caja para detectar el prefijo; preservamos el
    # subject original (no normalizamos a "Re: ").
    assert _compute_reply_subject("RE: Problema") == "RE: Problema"
    assert _compute_reply_subject("re: problema") == "re: problema"


def test_compute_subject_no_duplica_re_con_espacios():
    # "Re : Problema" tambien cuenta como prefijo (clientes que ponen espacio).
    assert _compute_reply_subject("Re : Problema") == "Re : Problema"


# ---------------------------------------------------------------------------
# Caso feliz
# ---------------------------------------------------------------------------


def test_send_reply_caso_feliz_invoca_send_persiste_y_actualiza_timestamp(
    conn: sqlite3.Connection, user: User
):
    ticket = _insert_ticket(conn)
    sender = _FakeEmailSender(message_id="<reply-001@telycoposio.com>")

    reply = send_reply(
        conn=conn,
        email_client=sender,
        ticket=ticket,
        body="Hola, estamos revisando tu caso.",
        user=user,
        now_fn=_frozen_now,
    )

    # ---- send se llamo con los args correctos ----
    assert len(sender.calls) == 1
    call = sender.calls[0]
    assert call["to"] == "cliente@example.com"
    assert call["subject"] == "Re: Problema con el router"
    assert call["body"] == "Hola, estamos revisando tu caso."
    assert call["in_reply_to"] == "<original-msg@cliente.com>"

    # ---- Reply devuelta con id asignado ----
    assert reply.id is not None
    assert reply.ticket_id == ticket.id
    assert reply.user_id == user.id
    assert reply.message_id == "<reply-001@telycoposio.com>"
    assert reply.sent_at == _FROZEN_NOW

    # ---- Persistida en BD ----
    replies = get_replies(conn, ticket.id)
    assert len(replies) == 1
    assert replies[0].id == reply.id

    # ---- ticket.last_updated_at actualizado, status sin cambiar ----
    updated_ticket = get_ticket(conn, ticket.id)
    assert updated_ticket is not None
    assert updated_ticket.last_updated_at == _FROZEN_NOW
    assert updated_ticket.status == TicketStatus.NEW

    # ---- synced_to_sheets_at NO se toca (sigue None) ----
    assert updated_ticket.synced_to_sheets_at is None


def test_send_reply_trim_del_body_se_aplica_antes_de_enviar(
    conn: sqlite3.Connection, user: User
):
    """Espacios y newlines del principio/final se eliminan antes de enviar
    y persistir. Asi un cuerpo accidentalmente con espacios no genera
    emails feos ni filas con whitespace inutil."""
    ticket = _insert_ticket(conn)
    sender = _FakeEmailSender()

    reply = send_reply(
        conn=conn,
        email_client=sender,
        ticket=ticket,
        body="   \n  Hola, gracias por contactar.  \n\n",
        user=user,
        now_fn=_frozen_now,
    )

    assert sender.calls[0]["body"] == "Hola, gracias por contactar."
    assert reply.body == "Hola, gracias por contactar."


def test_send_reply_con_new_status_cambia_estado(
    conn: sqlite3.Connection, user: User
):
    ticket = _insert_ticket(conn, status=TicketStatus.IN_PROGRESS)
    sender = _FakeEmailSender()

    send_reply(
        conn=conn,
        email_client=sender,
        ticket=ticket,
        body="Tu solicitud queda en espera.",
        user=user,
        new_status=TicketStatus.WAITING,
        now_fn=_frozen_now,
    )

    updated = get_ticket(conn, ticket.id)
    assert updated is not None
    assert updated.status == TicketStatus.WAITING


def test_send_reply_sin_new_status_no_cambia_estado(
    conn: sqlite3.Connection, user: User
):
    ticket = _insert_ticket(conn, status=TicketStatus.IN_PROGRESS)
    sender = _FakeEmailSender()

    send_reply(
        conn=conn,
        email_client=sender,
        ticket=ticket,
        body="Mensaje informativo.",
        user=user,
        new_status=None,
        now_fn=_frozen_now,
    )

    updated = get_ticket(conn, ticket.id)
    assert updated is not None
    assert updated.status == TicketStatus.IN_PROGRESS


def test_send_reply_subject_no_duplica_re_si_el_ticket_ya_lo_tenia(
    conn: sqlite3.Connection, user: User
):
    ticket = _insert_ticket(conn, subject="Re: Consulta")
    sender = _FakeEmailSender()

    send_reply(
        conn=conn,
        email_client=sender,
        ticket=ticket,
        body="...",
        user=user,
        now_fn=_frozen_now,
    )

    assert sender.calls[0]["subject"] == "Re: Consulta"


# ---------------------------------------------------------------------------
# Validaciones
# ---------------------------------------------------------------------------


def test_send_reply_body_vacio_lanza_value_error_sin_invocar_send(
    conn: sqlite3.Connection, user: User
):
    ticket = _insert_ticket(conn)
    sender = _FakeEmailSender()

    with pytest.raises(ValueError, match="body vacio"):
        send_reply(
            conn=conn,
            email_client=sender,
            ticket=ticket,
            body="",
            user=user,
            now_fn=_frozen_now,
        )
    assert sender.calls == []


def test_send_reply_body_solo_whitespace_lanza_value_error(
    conn: sqlite3.Connection, user: User
):
    ticket = _insert_ticket(conn)
    sender = _FakeEmailSender()

    with pytest.raises(ValueError, match="body vacio"):
        send_reply(
            conn=conn,
            email_client=sender,
            ticket=ticket,
            body="   \n  \t  ",
            user=user,
            now_fn=_frozen_now,
        )
    assert sender.calls == []


def test_send_reply_ticket_sin_from_email_lanza_value_error(
    conn: sqlite3.Connection, user: User
):
    ticket = _insert_ticket(conn, from_email=None)
    sender = _FakeEmailSender()

    with pytest.raises(ValueError, match="from_email"):
        send_reply(
            conn=conn,
            email_client=sender,
            ticket=ticket,
            body="...",
            user=user,
            now_fn=_frozen_now,
        )
    assert sender.calls == []


def test_send_reply_ticket_sin_raw_message_id_lanza_value_error(
    conn: sqlite3.Connection, user: User
):
    """Tickets demo o creados a mano sin raw_message_id no se pueden hilar."""
    ticket = _insert_ticket(conn, raw_message_id=None)
    sender = _FakeEmailSender()

    with pytest.raises(ValueError, match="raw_message_id"):
        send_reply(
            conn=conn,
            email_client=sender,
            ticket=ticket,
            body="...",
            user=user,
            now_fn=_frozen_now,
        )
    assert sender.calls == []


def test_send_reply_ticket_canal_no_email_lanza_value_error(
    conn: sqlite3.Connection, user: User
):
    """Futuros tickets de WhatsApp/voicemail no se responden por SMTP."""
    # Aunque WhatsApp aun no esta implementado, defendemos el invariante.
    ticket = _insert_ticket(
        conn,
        id="TLY-2026-0002",
        channel=TicketChannel.WHATSAPP,
        from_email=None,
        from_phone="+34600000000",
        raw_message_id=None,
    )
    sender = _FakeEmailSender()

    with pytest.raises(ValueError, match="canal"):
        send_reply(
            conn=conn,
            email_client=sender,
            ticket=ticket,
            body="...",
            user=user,
            now_fn=_frozen_now,
        )
    assert sender.calls == []


# ---------------------------------------------------------------------------
# Resiliencia: SMTP falla
# ---------------------------------------------------------------------------


def test_send_reply_smtp_lanza_propaga_y_bd_intacta(
    conn: sqlite3.Connection, user: User
):
    """Si SMTP falla, no queremos persistir nada: ni la reply ni el cambio
    de timestamp. El operador reintenta limpiamente.
    """
    ticket = _insert_ticket(conn)
    original_updated = ticket.last_updated_at
    sender = _FakeEmailSender(send_error=RuntimeError("smtp caido"))

    with pytest.raises(RuntimeError, match="smtp caido"):
        send_reply(
            conn=conn,
            email_client=sender,
            ticket=ticket,
            body="...",
            user=user,
            new_status=TicketStatus.WAITING,
            now_fn=_frozen_now,
        )

    # BD intacta: cero replies, last_updated_at original, status original.
    assert get_replies(conn, ticket.id) == []
    persisted = get_ticket(conn, ticket.id)
    assert persisted is not None
    assert persisted.last_updated_at == original_updated
    assert persisted.status == TicketStatus.NEW


# ---------------------------------------------------------------------------
# Multiples respuestas (historial)
# ---------------------------------------------------------------------------


def test_send_reply_acumula_historial_en_orden_cronologico(
    conn: sqlite3.Connection, user: User
):
    ticket = _insert_ticket(conn)
    sender = _FakeEmailSender(message_id="<first@x>")

    first = send_reply(
        conn=conn,
        email_client=sender,
        ticket=ticket,
        body="Primera respuesta.",
        user=user,
        now_fn=lambda: datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    sender.message_id = "<second@x>"
    second = send_reply(
        conn=conn,
        email_client=sender,
        ticket=ticket,
        body="Segunda respuesta.",
        user=user,
        now_fn=lambda: datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc),
    )

    replies = get_replies(conn, ticket.id)
    assert [r.id for r in replies] == [first.id, second.id]
    assert [r.body for r in replies] == ["Primera respuesta.", "Segunda respuesta."]
    assert isinstance(replies[0], Reply)
