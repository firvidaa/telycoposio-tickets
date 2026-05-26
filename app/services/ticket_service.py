"""Logica de negocio de tickets: creacion, generacion de IDs y consulta.

Este modulo es **puro SQLite**: no conoce Gmail, ni Anthropic, ni Sheets.
Recibe los datos ya parseados/clasificados y se encarga de:

1. Calcular el siguiente ID correlativo del anyo en zona Europe/Madrid
   (``TLY-2026-0001``, ``TLY-2026-0002``, ...).
2. Garantizar idempotencia por ``raw_message_id`` (si llega un duplicado
   simplemente devolvemos el ticket existente sin crear uno nuevo).
3. Calcular ``needs_review`` automaticamente segun la regla del SPEC §4.1:
   ``True`` si la IA fallo (``category IS NULL``) o si la confianza es
   menor que ``NEEDS_REVIEW_THRESHOLD`` (0.7).
4. Insertar el ticket dentro de una transaccion ``IMMEDIATE`` para que
   SELECT(MAX_ID) + INSERT no puedan ser pisados por otro writer
   (defensa barata aunque hoy solo haya un proceso).

Si el contador anual sobrepasa 9999 se levanta ``TicketIdOverflowError``,
una excepcion especifica para que el llamante pueda capturarla limpiamente
sin parsear mensajes.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone
from typing import Final
from zoneinfo import ZoneInfo

from app.db.sqlite import transaction
from app.models.reply import Reply, from_db_row as reply_from_db_row
from app.models.ticket import (
    TICKET_COLUMNS,
    Attachment,
    Ticket,
    TicketCategory,
    TicketChannel,
    TicketStatus,
    from_db_row,
    to_db_row,
)


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

#: Confianza minima para considerar la categoria de la IA "fiable".
#: Por debajo de este umbral el ticket queda con ``needs_review=True``.
NEEDS_REVIEW_THRESHOLD: Final[float] = 0.7

#: Numero maximo de tickets por anyo. Si se supera, levantamos
#: ``TicketIdOverflowError`` ruidosamente — preferimos fallar a mutar el
#: formato de ID a espaldas del usuario.
MAX_TICKETS_PER_YEAR: Final[int] = 9999

#: Prefijo por defecto. Para usar uno distinto, pasarlo via ``prefix=...``.
DEFAULT_TICKET_PREFIX: Final[str] = "TLY"

#: Zona horaria local de Telycoposio. El anyo de un ticket se calcula segun
#: la fecha que verian los empleados, no segun UTC.
LOCAL_TZ = ZoneInfo("Europe/Madrid")

# Patron del ID: ``TLY-2026-0001``. Los grupos son prefijo, anyo y secuencia.
_ID_RE: Final = re.compile(r"^([A-Z]+)-(\d{4})-(\d{4})$")


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Excepciones
# ---------------------------------------------------------------------------


class TicketIdOverflowError(Exception):
    """El contador anual de tickets ha superado ``MAX_TICKETS_PER_YEAR``.

    Si se llega aqui, el formato ``NNNN`` se quedo corto: replantear si el
    crecimiento del negocio justifica un formato mas ancho.
    """


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------


def _local_year(dt: datetime) -> int:
    """Devuelve el anyo en zona Europe/Madrid de un datetime cualquiera.

    Si ``dt`` es naive, asumimos UTC.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ).year


def _next_ticket_id(
    conn: sqlite3.Connection,
    *,
    year: int,
    prefix: str,
) -> str:
    """Calcula el siguiente ID correlativo para el anyo dado.

    Debe llamarse **dentro** de una transaccion ``IMMEDIATE`` para evitar
    carreras: el SELECT y el INSERT que sigue tienen que verse coherentes.

    Lanza :class:`TicketIdOverflowError` si el siguiente numero supera
    ``MAX_TICKETS_PER_YEAR``.
    """
    pattern = f"{prefix}-{year:04d}-%"
    row = conn.execute(
        "SELECT id FROM tickets WHERE id LIKE ? ORDER BY id DESC LIMIT 1",
        (pattern,),
    ).fetchone()

    if row is None:
        next_n = 1
    else:
        match = _ID_RE.match(row["id"])
        if match is None:
            # Solo puede pasar si alguien metio un ID que no respeta el
            # formato a mano. Mejor fallar ruidosamente.
            raise RuntimeError(
                f"ID con formato inesperado en BD: {row['id']!r}. "
                "El esquema de IDs ha sido violado."
            )
        next_n = int(match.group(3)) + 1

    if next_n > MAX_TICKETS_PER_YEAR:
        raise TicketIdOverflowError(
            f"Se ha alcanzado el limite de {MAX_TICKETS_PER_YEAR} tickets "
            f"en el anyo {year}. Replantear el formato de ID o el crecimiento."
        )

    return f"{prefix}-{year:04d}-{next_n:04d}"


def _compute_needs_review(
    category: TicketCategory | None,
    category_confidence: float | None,
) -> bool:
    """Implementa la regla del SPEC §4.1.

    ``True`` si:
    - la IA fallo (``category is None``), o
    - no se pudo medir la confianza (``category_confidence is None``), o
    - la confianza es estrictamente menor que ``NEEDS_REVIEW_THRESHOLD``.

    En el threshold exacto (0.7) **no** marcamos para revision: la regla
    del SPEC dice "menor que 0.7", no "menor o igual".
    """
    if category is None:
        return True
    if category_confidence is None:
        return True
    return category_confidence < NEEDS_REVIEW_THRESHOLD


# ---------------------------------------------------------------------------
# API publica
# ---------------------------------------------------------------------------


def create_ticket(
    *,
    conn: sqlite3.Connection,
    channel: TicketChannel,
    subject: str,
    body: str,
    from_name: str | None = None,
    from_email: str | None = None,
    from_phone: str | None = None,
    raw_message_id: str | None = None,
    attachments: list[Attachment] | None = None,
    category: TicketCategory | None = None,
    category_confidence: float | None = None,
    category_reasoning: str | None = None,
    now: datetime | None = None,
    prefix: str = DEFAULT_TICKET_PREFIX,
) -> Ticket:
    """Crea un ticket nuevo y lo inserta en SQLite.

    Idempotente respecto a ``raw_message_id``: si ya existe un ticket con ese
    valor, devolvemos el existente (sin crear uno nuevo) y registramos un log
    informativo.

    ``now`` se inyecta principalmente para tests; si no se pasa, usamos
    ``datetime.now(UTC)``. El **anyo del ID** se calcula en zona Europe/Madrid
    a partir de ``now`` (ver ``_local_year``).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # ---- Dedup por raw_message_id (idempotencia natural) ----
    if raw_message_id is not None:
        existing = conn.execute(
            "SELECT * FROM tickets WHERE raw_message_id = ?",
            (raw_message_id,),
        ).fetchone()
        if existing is not None:
            existing_ticket = from_db_row(existing)
            logger.info(
                "duplicado descartado: raw_message_id=%r ya existia como %s",
                raw_message_id,
                existing_ticket.id,
            )
            return existing_ticket

    year = _local_year(now)
    needs_review = _compute_needs_review(category, category_confidence)

    # ---- SELECT(MAX_ID)+INSERT en transaccion IMMEDIATE ----
    with transaction(conn, mode="IMMEDIATE"):
        ticket_id = _next_ticket_id(conn, year=year, prefix=prefix)

        ticket = Ticket(
            id=ticket_id,
            created_at=now,
            channel=channel,
            from_name=from_name,
            from_email=from_email,
            from_phone=from_phone,
            subject=subject,
            body=body,
            status=TicketStatus.NEW,
            category=category,
            category_confidence=category_confidence,
            category_reasoning=category_reasoning,
            category_manual_override=False,
            needs_review=needs_review,
            raw_message_id=raw_message_id,
            attachments=attachments or [],
            client_notified_at=None,
            last_updated_at=now,
            synced_to_sheets_at=None,
        )

        row = to_db_row(ticket)
        placeholders = ", ".join(f":{c}" for c in TICKET_COLUMNS)
        conn.execute(
            f"INSERT INTO tickets ({', '.join(TICKET_COLUMNS)}) "
            f"VALUES ({placeholders})",
            row,
        )

    return ticket


def get_ticket(conn: sqlite3.Connection, ticket_id: str) -> Ticket | None:
    """Recupera un ticket por su ID. Devuelve ``None`` si no existe."""
    row = conn.execute(
        "SELECT * FROM tickets WHERE id = ?",
        (ticket_id,),
    ).fetchone()
    if row is None:
        return None
    return from_db_row(row)


# ---------------------------------------------------------------------------
# Listado y conteo
# ---------------------------------------------------------------------------

#: Limites de paginacion. La ruta los acota antes; aqui solo aceptamos lo
#: que pase el llamante (las rutas usan ``Query(..., ge=1, le=200)``).
DEFAULT_LIST_LIMIT: Final[int] = 50
MAX_LIST_LIMIT: Final[int] = 200


def _build_filters(
    *,
    statuses: list[TicketStatus] | None,
    category: TicketCategory | None,
    only_uncategorized: bool,
    needs_review: bool | None,
) -> tuple[str, list[object]]:
    """Construye la clausula WHERE y la lista de args para los filtros dados.

    ``statuses=None`` significa "no filtrar por status"; ``[]`` (lista vacia)
    significa "ninguno coincide" — devolvemos un WHERE imposible para que el
    resultado sea vacio sin ramas adicionales.
    """
    clauses: list[str] = []
    args: list[object] = []

    if statuses is not None:
        if not statuses:
            return ("WHERE 1 = 0", [])
        placeholders = ",".join(["?"] * len(statuses))
        clauses.append(f"status IN ({placeholders})")
        args.extend(s.value for s in statuses)

    if only_uncategorized:
        clauses.append("category IS NULL")
    elif category is not None:
        clauses.append("category = ?")
        args.append(category.value)

    if needs_review is not None:
        clauses.append("needs_review = ?")
        args.append(int(needs_review))

    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    return (where, args)


def list_tickets(
    conn: sqlite3.Connection,
    *,
    statuses: list[TicketStatus] | None = None,
    category: TicketCategory | None = None,
    only_uncategorized: bool = False,
    needs_review: bool | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
) -> list[Ticket]:
    """Lista tickets ordenados por ``created_at DESC`` con filtros y paginacion.

    Filtros:
    - ``statuses``: lista blanca a filtrar con ``status IN (...)``. ``None``
      no filtra; lista vacia devuelve resultado vacio.
    - ``category`` vs. ``only_uncategorized``: mutuamente excluyentes en la
      practica. Si ``only_uncategorized=True`` ganamos esa rama y se ignora
      ``category``.
    - ``needs_review``: ``True`` / ``False`` / ``None`` (no filtrar).

    La ruta valida ``limit`` y ``offset`` con ``Query(..., ge=..., le=...)``;
    aqui no re-validamos para no duplicar la regla.
    """
    where, args = _build_filters(
        statuses=statuses,
        category=category,
        only_uncategorized=only_uncategorized,
        needs_review=needs_review,
    )
    sql = (
        f"SELECT * FROM tickets {where} "
        "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    )
    rows = conn.execute(sql, [*args, limit, offset]).fetchall()
    return [from_db_row(r) for r in rows]


def count_tickets(
    conn: sqlite3.Connection,
    *,
    statuses: list[TicketStatus] | None = None,
    category: TicketCategory | None = None,
    only_uncategorized: bool = False,
    needs_review: bool | None = None,
) -> int:
    """Cuenta tickets que cumplen los mismos filtros que ``list_tickets``.

    No la usamos para mostrar "X de N" en el listado (decision: paginacion
    sin total para evitar un ``COUNT(*)`` por request), pero si para tests
    y para el script de seed.
    """
    where, args = _build_filters(
        statuses=statuses,
        category=category,
        only_uncategorized=only_uncategorized,
        needs_review=needs_review,
    )
    row = conn.execute(f"SELECT COUNT(*) AS n FROM tickets {where}", args).fetchone()
    return int(row["n"])


# ---------------------------------------------------------------------------
# Respuestas (Paso 10)
# ---------------------------------------------------------------------------


def get_replies(conn: sqlite3.Connection, ticket_id: str) -> list[Reply]:
    """Lista las respuestas de un ticket en orden cronologico ASC.

    No filtra si el ticket existe o no: devuelve lista vacia tanto si no
    hay respuestas como si el ticket no existe. La ruta web ya valida la
    existencia antes de invocar.
    """
    rows = conn.execute(
        "SELECT * FROM ticket_replies WHERE ticket_id = ? "
        "ORDER BY sent_at ASC, id ASC",
        (ticket_id,),
    ).fetchall()
    return [reply_from_db_row(r) for r in rows]
