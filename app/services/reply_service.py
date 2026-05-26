"""Orquesta el envio de una respuesta al cliente desde la web (Paso 10).

Flujo:

1. Validar precondiciones (body no vacio, ticket con email y
   ``raw_message_id``, canal email).
2. Calcular el ``Subject`` final (anyade ``Re: `` solo si no estaba).
3. Llamar a ``email_client.send(..., in_reply_to=ticket.raw_message_id)``.
   Si falla, propagar la excepcion (NO se persiste nada).
4. Persistir la ``Reply`` en BD y actualizar ``ticket.last_updated_at``
   y opcionalmente ``ticket.status`` en una unica transaccion.

Decisiones (ver discusion del Paso 10):

- **Una respuesta es inmutable**: una vez enviada+persistida no se edita
  ni se borra desde la UI.
- **Sin persistencia previa**: si SMTP falla, no queremos huerfanos en
  BD. Mismo tradeoff que el poller del Paso 8 (capas idempotentes).
- **``new_status=None`` = no cambia el estado.** El form de la UI sugiere
  ``WAITING`` por default pero el operador puede dejar el original o
  pasar a ``CLOSED`` directamente.
- **No tocamos ``synced_to_sheets_at``**: al cambiar ``last_updated_at``,
  el sync del Paso 9 detecta el ticket como pendiente automaticamente.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol

from app.db.sqlite import transaction
from app.models.reply import REPLY_COLUMNS, Reply, to_db_row as reply_to_db_row
from app.models.ticket import Ticket, TicketChannel, TicketStatus
from app.models.user import User


logger = logging.getLogger(__name__)


#: Coincide con ``Re:`` opcionalmente seguido de espacios y mas ``Re:`` (RE:,
#: re:, Re : ...). Solo nos importa que no dupliquemos el prefijo en el
#: subject saliente. No quitamos los anteriores si el cliente los anyadio.
_RE_PREFIX = re.compile(r"^\s*re\s*:\s*", re.IGNORECASE)


class EmailSender(Protocol):
    """Subset minimo de ``EmailClient`` que usa el servicio."""

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        in_reply_to: str | None = None,
    ) -> str: ...


NowFn = Callable[[], datetime]


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _compute_reply_subject(original_subject: str) -> str:
    """Anyade ``Re: `` salvo que ya empezara por ``Re:`` (case-insensitive).

    No removemos prefijos previos del cliente (``Fwd:``, ``RE:``, etc.):
    solo evitamos generar ``Re: Re: ...`` desde nuestro lado.
    """
    if _RE_PREFIX.match(original_subject):
        return original_subject
    return f"Re: {original_subject}"


def send_reply(
    *,
    conn: sqlite3.Connection,
    email_client: EmailSender,
    ticket: Ticket,
    body: str,
    user: User,
    new_status: TicketStatus | None = None,
    now_fn: NowFn = _default_now,
) -> Reply:
    """Envia una respuesta al cliente y la persiste.

    Lanza ``ValueError`` si las precondiciones no se cumplen (cuerpo
    vacio, ticket sin email o sin ``raw_message_id``, canal != email).
    Propaga cualquier excepcion del cliente SMTP sin persistir nada.

    Devuelve la ``Reply`` ya con ``id`` asignado por SQLite.
    """
    body_stripped = body.strip()
    if not body_stripped:
        raise ValueError("body vacio: no se puede enviar una respuesta sin contenido.")
    if ticket.channel != TicketChannel.EMAIL:
        raise ValueError(
            f"canal {ticket.channel.value!r}: solo se puede responder a tickets de email."
        )
    if not ticket.from_email:
        raise ValueError("ticket sin from_email: no hay destinatario para la respuesta.")
    if not ticket.raw_message_id:
        raise ValueError(
            "ticket sin raw_message_id: no se puede hilar la respuesta. "
            "Probablemente es un ticket demo o creado a mano."
        )
    if user.id is None:
        # No deberia pasar (usuarios persistidos tienen id), defensa.
        raise ValueError("user sin id: no se puede atribuir la respuesta.")

    subject_out = _compute_reply_subject(ticket.subject)

    # ---- SMTP fuera de la transaccion: si falla, BD intacta ----
    message_id = email_client.send(
        to=ticket.from_email,
        subject=subject_out,
        body=body_stripped,
        in_reply_to=ticket.raw_message_id,
    )

    now = now_fn()

    # ---- INSERT reply + UPDATE ticket en una transaccion atomica ----
    with transaction(conn, mode="IMMEDIATE"):
        reply = Reply(
            ticket_id=ticket.id,
            sent_at=now,
            user_id=user.id,
            body=body_stripped,
            message_id=message_id,
        )
        row = reply_to_db_row(reply)
        placeholders = ", ".join(f":{c}" for c in REPLY_COLUMNS)
        cur = conn.execute(
            f"INSERT INTO ticket_replies ({', '.join(REPLY_COLUMNS)}) "
            f"VALUES ({placeholders})",
            row,
        )
        reply_id = cur.lastrowid

        # Actualizamos last_updated_at siempre. Si new_status no es None,
        # tambien el status. No tocamos synced_to_sheets_at: el sync del
        # Paso 9 detecta la diferencia y refresca la fila.
        if new_status is not None:
            conn.execute(
                "UPDATE tickets SET last_updated_at = ?, status = ? WHERE id = ?",
                (_iso_utc(now), new_status.value, ticket.id),
            )
        else:
            conn.execute(
                "UPDATE tickets SET last_updated_at = ? WHERE id = ?",
                (_iso_utc(now), ticket.id),
            )

    logger.info(
        "reply_service exito ticket=%s reply_id=%s message_id=%s new_status=%s",
        ticket.id,
        reply_id,
        message_id,
        new_status.value if new_status is not None else "-",
    )
    return reply.model_copy(update={"id": reply_id})
