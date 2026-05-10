"""Tests del script ``scripts/seed_demo_tickets.py``.

Importamos la funcion ``seed`` (no el ``main`` de CLI) y la ejercitamos sobre
una BD ``:memory:``. Cubrimos las ramas: insercion limpia, idempotencia,
``--reset``, proteccion contra mezclar con datos reales y desbloqueo via
``--force``.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

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


# Cargamos el script como modulo. ``scripts/`` no es un paquete (no tiene
# ``__init__.py``); usamos importlib para no depender de PYTHONPATH.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SEED_PATH = _PROJECT_ROOT / "scripts" / "seed_demo_tickets.py"
_spec = importlib.util.spec_from_file_location("seed_demo_tickets", _SEED_PATH)
assert _spec is not None and _spec.loader is not None
seed_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_module)

DEMO_EMAIL = seed_module.DEMO_EMAIL
SeedAbortError = seed_module.SeedAbortError
seed = seed_module.seed


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    with get_connection(":memory:") as c:
        init_schema(c)
        yield c


def _insert_real_ticket(conn: sqlite3.Connection) -> None:
    t = Ticket(
        id="TLY-2026-0001",
        created_at=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
        channel=TicketChannel.EMAIL,
        from_email="cliente.real@example.com",
        subject="Real",
        body="cuerpo",
        status=TicketStatus.NEW,
        category=TicketCategory.SOPORTE,
        category_confidence=0.9,
        needs_review=False,
        last_updated_at=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
    )
    placeholders = ", ".join(f":{c}" for c in TICKET_COLUMNS)
    conn.execute(
        f"INSERT INTO tickets ({', '.join(TICKET_COLUMNS)}) VALUES ({placeholders})",
        to_db_row(t),
    )


def _count_demo(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM tickets WHERE from_email = ?",
        (DEMO_EMAIL,),
    ).fetchone()
    return int(row["n"])


# ---------------------------------------------------------------------------
# Insercion limpia
# ---------------------------------------------------------------------------


def test_seed_inserta_tickets_demo(conn: sqlite3.Connection) -> None:
    res = seed(conn)
    assert res["created"] == 7
    assert res["existing"] == 0
    assert _count_demo(conn) == 7


def test_seed_genera_variedad_para_filtros(conn: sqlite3.Connection) -> None:
    """Los datos demo deben cubrir los filtros visibles del listado."""
    seed(conn)

    # Cada categoria al menos una vez.
    rows = conn.execute(
        "SELECT DISTINCT category FROM tickets WHERE from_email = ?",
        (DEMO_EMAIL,),
    ).fetchall()
    categorias = {r["category"] for r in rows}
    assert {"ADMINISTRATIVO", "COMERCIAL", "SOPORTE"} <= categorias
    assert None in categorias  # un ticket sin clasificar

    # Cada status al menos una vez.
    rows = conn.execute(
        "SELECT DISTINCT status FROM tickets WHERE from_email = ?",
        (DEMO_EMAIL,),
    ).fetchall()
    statuses = {r["status"] for r in rows}
    assert statuses == {"NEW", "IN_PROGRESS", "WAITING", "CLOSED"}

    # Al menos un ticket needs_review.
    n_review = conn.execute(
        "SELECT COUNT(*) AS n FROM tickets "
        "WHERE from_email = ? AND needs_review = 1",
        (DEMO_EMAIL,),
    ).fetchone()["n"]
    assert n_review >= 2  # 1 con category=NULL + 1 con confidence < 0.7

    # Al menos un ticket con attachments.
    n_attach = conn.execute(
        "SELECT COUNT(*) AS n FROM tickets "
        "WHERE from_email = ? AND attachments != '[]'",
        (DEMO_EMAIL,),
    ).fetchone()["n"]
    assert n_attach >= 1


def test_seed_marca_canonica(conn: sqlite3.Connection) -> None:
    """Todos los tickets demo llevan ``from_email='demo@ejemplo.com'`` y
    ``[DEMO]`` en el subject (las dos marcas que documenta el script)."""
    seed(conn)
    rows = conn.execute(
        "SELECT subject FROM tickets WHERE from_email = ?",
        (DEMO_EMAIL,),
    ).fetchall()
    assert len(rows) == 7
    assert all(r["subject"].startswith("[DEMO]") for r in rows)


# ---------------------------------------------------------------------------
# Idempotencia y --reset
# ---------------------------------------------------------------------------


def test_seed_es_idempotente(conn: sqlite3.Connection) -> None:
    seed(conn)
    res = seed(conn)
    assert res["created"] == 0
    assert res["existing"] == 7
    assert _count_demo(conn) == 7  # sigue habiendo 7, no 14


def test_seed_reset_borra_y_recrea(conn: sqlite3.Connection) -> None:
    seed(conn)
    assert _count_demo(conn) == 7

    res = seed(conn, reset=True)
    assert res["deleted"] == 7
    assert res["created"] == 7
    assert _count_demo(conn) == 7


# ---------------------------------------------------------------------------
# Proteccion contra mezclar con datos reales
# ---------------------------------------------------------------------------


def test_seed_aborta_si_hay_tickets_no_demo(conn: sqlite3.Connection) -> None:
    _insert_real_ticket(conn)
    with pytest.raises(SeedAbortError):
        seed(conn)
    # No se insertan demos cuando aborta.
    assert _count_demo(conn) == 0


def test_seed_force_desbloquea(conn: sqlite3.Connection) -> None:
    _insert_real_ticket(conn)
    res = seed(conn, force=True)
    assert res["created"] == 7
    assert _count_demo(conn) == 7
    # El real sigue ahi.
    real = conn.execute(
        "SELECT COUNT(*) AS n FROM tickets WHERE from_email != ?",
        (DEMO_EMAIL,),
    ).fetchone()["n"]
    assert real == 1


def test_seed_force_idempotente_si_ya_hay_demos(
    conn: sqlite3.Connection,
) -> None:
    """Con --force, si ya hay demos sigue siendo idempotente (no duplica)."""
    _insert_real_ticket(conn)
    seed(conn, force=True)
    res = seed(conn, force=True)
    assert res["created"] == 0
    assert res["existing"] == 7
