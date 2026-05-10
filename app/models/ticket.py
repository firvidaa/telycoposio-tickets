"""Modelo de ticket y conversion a/desde fila SQLite.

El modelo Pydantic es la representacion canonica usada por el resto del codigo.
``to_db_row`` y ``from_db_row`` son las unicas funciones que conocen los
detalles de almacenamiento (booleans como 0/1, datetimes como ISO-8601 UTC,
adjuntos como JSON serializado).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Enumeraciones
# ---------------------------------------------------------------------------


class TicketStatus(str, Enum):
    NEW = "NEW"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING = "WAITING"
    CLOSED = "CLOSED"


class TicketCategory(str, Enum):
    ADMINISTRATIVO = "ADMINISTRATIVO"
    COMERCIAL = "COMERCIAL"
    SOPORTE = "SOPORTE"


class TicketChannel(str, Enum):
    EMAIL = "email"
    WHATSAPP = "whatsapp"
    VOICEMAIL = "voicemail"


# ---------------------------------------------------------------------------
# Submodelos
# ---------------------------------------------------------------------------


class Attachment(BaseModel):
    """Adjunto referenciado desde un ticket.

    En MVP (v1.3) ``url`` es ``None`` porque el cliente IMAP solo extrae
    metadatos (no descarga el contenido). ``size_bytes`` es el tamano del
    payload decodificado del MIME part. Cuando en el futuro guardemos el
    contenido en almacenamiento accesible, ``url`` apuntara ahi.
    """

    name: str
    size_bytes: int | None = None
    url: str | None = None


# ---------------------------------------------------------------------------
# Modelo principal
# ---------------------------------------------------------------------------


class Ticket(BaseModel):
    """Representacion en memoria de un ticket. Ver SPEC.md §4.1 (v1.2)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    created_at: datetime
    channel: TicketChannel
    from_name: str | None = None
    from_email: str | None = None
    from_phone: str | None = None
    subject: str
    body: str
    status: TicketStatus = TicketStatus.NEW
    category: TicketCategory | None = None
    category_confidence: float | None = None
    category_reasoning: str | None = None
    category_manual_override: bool = False
    needs_review: bool = False
    raw_message_id: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    client_notified_at: datetime | None = None
    last_updated_at: datetime
    synced_to_sheets_at: datetime | None = None


# ---------------------------------------------------------------------------
# Conversion BD <-> modelo
# ---------------------------------------------------------------------------


def _iso_utc(dt: datetime | None) -> str | None:
    """Serializa un datetime a ISO-8601 UTC. ``None`` se preserva."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    """Parsea ISO-8601 a datetime. ``None`` se preserva."""
    if value is None:
        return None
    return datetime.fromisoformat(value)


def to_db_row(ticket: Ticket) -> dict[str, Any]:
    """Convierte un ``Ticket`` al diccionario que espera el ``INSERT``/``UPDATE``."""
    return {
        "id": ticket.id,
        "created_at": _iso_utc(ticket.created_at),
        "channel": ticket.channel.value,
        "from_name": ticket.from_name,
        "from_email": ticket.from_email,
        "from_phone": ticket.from_phone,
        "subject": ticket.subject,
        "body": ticket.body,
        "status": ticket.status.value,
        "category": ticket.category.value if ticket.category is not None else None,
        "category_confidence": ticket.category_confidence,
        "category_reasoning": ticket.category_reasoning,
        "category_manual_override": int(ticket.category_manual_override),
        "needs_review": int(ticket.needs_review),
        "raw_message_id": ticket.raw_message_id,
        "attachments": json.dumps(
            [a.model_dump() for a in ticket.attachments],
            ensure_ascii=False,
        ),
        "client_notified_at": _iso_utc(ticket.client_notified_at),
        "last_updated_at": _iso_utc(ticket.last_updated_at),
        "synced_to_sheets_at": _iso_utc(ticket.synced_to_sheets_at),
    }


def from_db_row(row: sqlite3.Row) -> Ticket:
    """Reconstruye un ``Ticket`` a partir de una fila ``sqlite3.Row``."""
    attachments_raw = row["attachments"] or "[]"
    return Ticket(
        id=row["id"],
        created_at=_parse_iso(row["created_at"]),  # type: ignore[arg-type]
        channel=TicketChannel(row["channel"]),
        from_name=row["from_name"],
        from_email=row["from_email"],
        from_phone=row["from_phone"],
        subject=row["subject"],
        body=row["body"],
        status=TicketStatus(row["status"]),
        category=TicketCategory(row["category"]) if row["category"] is not None else None,
        category_confidence=row["category_confidence"],
        category_reasoning=row["category_reasoning"],
        category_manual_override=bool(row["category_manual_override"]),
        needs_review=bool(row["needs_review"]),
        raw_message_id=row["raw_message_id"],
        attachments=[Attachment(**a) for a in json.loads(attachments_raw)],
        client_notified_at=_parse_iso(row["client_notified_at"]),
        last_updated_at=_parse_iso(row["last_updated_at"]),  # type: ignore[arg-type]
        synced_to_sheets_at=_parse_iso(row["synced_to_sheets_at"]),
    )


# Lista canonica de columnas en el orden de schema.sql. La usan los INSERT.
TICKET_COLUMNS: tuple[str, ...] = (
    "id",
    "created_at",
    "channel",
    "from_name",
    "from_email",
    "from_phone",
    "subject",
    "body",
    "status",
    "category",
    "category_confidence",
    "category_reasoning",
    "category_manual_override",
    "needs_review",
    "raw_message_id",
    "attachments",
    "client_notified_at",
    "last_updated_at",
    "synced_to_sheets_at",
)
