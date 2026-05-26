"""Modelo de respuesta enviada al cliente (Paso 10).

Una ``Reply`` es inmutable: se inserta cuando el envio SMTP confirma OK
y no se edita ni se borra desde la UI. ``message_id`` es el header
``Message-Id`` que SMTP genero al enviar (lo devuelve ``email_client.send``).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict


class Reply(BaseModel):
    """Representacion en memoria de una respuesta enviada al cliente."""

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    ticket_id: str
    sent_at: datetime
    user_id: int
    body: str
    message_id: str


def _iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def to_db_row(reply: Reply) -> dict[str, Any]:
    """Diccionario para ``INSERT INTO ticket_replies (...)``.

    No incluye ``id`` (es ``AUTOINCREMENT``).
    """
    return {
        "ticket_id": reply.ticket_id,
        "sent_at": _iso_utc(reply.sent_at),
        "user_id": reply.user_id,
        "body": reply.body,
        "message_id": reply.message_id,
    }


def from_db_row(row: sqlite3.Row) -> Reply:
    """Reconstruye una ``Reply`` desde una fila ``sqlite3.Row``."""
    return Reply(
        id=row["id"],
        ticket_id=row["ticket_id"],
        sent_at=datetime.fromisoformat(row["sent_at"]),
        user_id=row["user_id"],
        body=row["body"],
        message_id=row["message_id"],
    )


REPLY_COLUMNS: tuple[str, ...] = (
    "ticket_id",
    "sent_at",
    "user_id",
    "body",
    "message_id",
)
