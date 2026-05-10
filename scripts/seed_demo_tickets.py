"""Inserta tickets de demostracion en la BD para poder probar la UI sin tener
que esperar al worker de Gmail (Paso 5b).

Marca demo: ``from_email = 'demo@ejemplo.com'`` y subject con prefijo
``[DEMO]``. Eso permite purgar limpiamente con un solo ``WHERE``.

Uso:
    python scripts/seed_demo_tickets.py                # idempotente
    python scripts/seed_demo_tickets.py --reset        # borra demos y recrea
    python scripts/seed_demo_tickets.py --force        # permite mezclar con datos reales

Comportamiento:

- Si la BD tiene tickets que **no** son demo, aborta. Solo ``--force`` permite
  inyectar la semilla en una BD con datos reales (util si quieres una demo en
  un servidor que ya este en uso).
- Sin flags: si ya hay tickets demo, no hace nada (idempotente). Si no hay,
  los crea.
- ``--reset``: borra los tickets demo existentes y los vuelve a crear.

Variedad de los 7 tickets sembrados:
- Cada categoria (ADMINISTRATIVO, COMERCIAL, SOPORTE).
- Cada status (NEW, IN_PROGRESS, WAITING, CLOSED).
- Un ticket con ``category=NULL`` y ``needs_review=True`` (la IA fallo).
- Un ticket con ``category_confidence < 0.7`` -> ``needs_review=True``.
- Un ticket con ``attachments`` para verificar el round-trip JSON en el detalle.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from app.db.sqlite import get_connection, transaction  # noqa: E402
from app.models.ticket import (  # noqa: E402
    TICKET_COLUMNS,
    Attachment,
    Ticket,
    TicketCategory,
    TicketChannel,
    TicketStatus,
    to_db_row,
)


DEMO_EMAIL = "demo@ejemplo.com"
DEMO_SUBJECT_PREFIX = "[DEMO]"


class SeedAbortError(Exception):
    """La BD contiene tickets reales y no se paso ``--force``."""


def _demo_tickets(now: datetime | None = None) -> list[Ticket]:
    """Devuelve la lista canonica de tickets demo (siempre la misma).

    ``now`` se inyecta para tests; en uso normal usa la hora actual UTC. Las
    fechas de cada ticket se escalonan en minutos para que el orden por
    ``created_at DESC`` produzca un listado predecible.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    def at(min_ago: int) -> datetime:
        return now - timedelta(minutes=min_ago)

    base = dict(channel=TicketChannel.EMAIL, from_email=DEMO_EMAIL)

    return [
        Ticket(
            **base,
            id="TLY-2026-9991",
            created_at=at(60),
            from_name="Demo Cliente A",
            subject=f"{DEMO_SUBJECT_PREFIX} Solicitud de factura proforma",
            body="Buenos dias,\n\nNecesito una factura proforma para el pedido del mes pasado.\n\nSaludos.",
            status=TicketStatus.NEW,
            category=TicketCategory.ADMINISTRATIVO,
            category_confidence=0.95,
            category_reasoning="Mencion explicita de 'factura proforma'.",
            needs_review=False,
            last_updated_at=at(60),
        ),
        Ticket(
            **base,
            id="TLY-2026-9992",
            created_at=at(50),
            from_name="Demo Cliente B",
            subject=f"{DEMO_SUBJECT_PREFIX} Presupuesto router 4G",
            body="Hola, querria un presupuesto de un router 4G para una segunda residencia.",
            status=TicketStatus.IN_PROGRESS,
            category=TicketCategory.COMERCIAL,
            category_confidence=0.88,
            category_reasoning="Consulta de presupuesto de producto.",
            needs_review=False,
            last_updated_at=at(40),
        ),
        Ticket(
            **base,
            id="TLY-2026-9993",
            created_at=at(40),
            from_name="Demo Cliente C",
            subject=f"{DEMO_SUBJECT_PREFIX} El telefono IP no da tono",
            body="Buenos dias,\n\nDesde ayer el telefono de recepcion no da tono al descolgar.\nLa luz esta encendida pero no suena.",
            status=TicketStatus.WAITING,
            category=TicketCategory.SOPORTE,
            category_confidence=0.92,
            category_reasoning="Incidencia tecnica con telefono IP.",
            needs_review=False,
            last_updated_at=at(35),
        ),
        Ticket(
            **base,
            id="TLY-2026-9994",
            created_at=at(30),
            from_name="Demo Cliente D",
            subject=f"{DEMO_SUBJECT_PREFIX} Problema con la impresora — resuelto",
            body="Gracias, ya funciona despues de reiniciar el switch.",
            status=TicketStatus.CLOSED,
            category=TicketCategory.SOPORTE,
            category_confidence=0.99,
            category_reasoning="Cierre de incidencia resuelta por el propio cliente.",
            needs_review=False,
            last_updated_at=at(25),
        ),
        Ticket(
            **base,
            id="TLY-2026-9995",
            created_at=at(20),
            from_name="Demo Cliente E",
            subject=f"{DEMO_SUBJECT_PREFIX} Mensaje breve sin contexto",
            body="hola que tal",
            status=TicketStatus.NEW,
            category=None,
            category_confidence=None,
            category_reasoning=None,
            needs_review=True,
            last_updated_at=at(20),
        ),
        Ticket(
            **base,
            id="TLY-2026-9996",
            created_at=at(10),
            from_name="Demo Cliente F",
            subject=f"{DEMO_SUBJECT_PREFIX} Quiero saber mas sobre el servicio",
            body="Hola, me han hablado de vosotros, me gustaria saber que ofreceis para una empresa pequenya.",
            status=TicketStatus.NEW,
            category=TicketCategory.COMERCIAL,
            category_confidence=0.55,
            category_reasoning="Tono dudoso: podria ser comercial o administrativo.",
            needs_review=True,
            last_updated_at=at(10),
        ),
        Ticket(
            **base,
            id="TLY-2026-9997",
            created_at=at(5),
            from_name="Demo Cliente G",
            subject=f"{DEMO_SUBJECT_PREFIX} Adjunto la captura del error",
            body="Os mando la captura del error que aparece al abrir el correo.",
            status=TicketStatus.IN_PROGRESS,
            category=TicketCategory.SOPORTE,
            category_confidence=0.91,
            category_reasoning="Incidencia con cliente de email; usuario adjunta captura.",
            needs_review=False,
            attachments=[
                # En MVP (v1.3) solo guardamos metadatos. ``url`` queda en None
                # porque no descargamos el contenido; el detalle muestra
                # nombre + tamanyo sin link.
                Attachment(name="captura_error.png", size_bytes=58_400),
                Attachment(name="log.txt", size_bytes=1_240),
            ],
            last_updated_at=at(5),
        ),
    ]


def _count_real(conn: sqlite3.Connection) -> int:
    """Tickets que no llevan la marca de demo."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM tickets "
        "WHERE from_email IS NULL OR from_email != ?",
        (DEMO_EMAIL,),
    ).fetchone()
    return int(row["n"])


def _count_demo(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM tickets WHERE from_email = ?",
        (DEMO_EMAIL,),
    ).fetchone()
    return int(row["n"])


def _insert_demo_batch(
    conn: sqlite3.Connection, tickets: list[Ticket]
) -> int:
    placeholders = ", ".join(f":{c}" for c in TICKET_COLUMNS)
    sql = (
        f"INSERT INTO tickets ({', '.join(TICKET_COLUMNS)}) VALUES ({placeholders})"
    )
    with transaction(conn, mode="IMMEDIATE"):
        for t in tickets:
            conn.execute(sql, to_db_row(t))
    return len(tickets)


def seed(
    conn: sqlite3.Connection,
    *,
    reset: bool = False,
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, int]:
    """Logica del seed. Devuelve ``{'created', 'deleted', 'existing'}``.

    - ``existing > 0`` cuando ya habia demos y no se hizo reset (idempotente).
    - ``deleted`` solo > 0 si se paso ``reset=True``.

    Lanza :class:`SeedAbortError` si hay tickets reales y no se paso ``force``.
    """
    real = _count_real(conn)
    if real > 0 and not force:
        raise SeedAbortError(
            f"La BD contiene {real} tickets que no son demo "
            f"(from_email != '{DEMO_EMAIL}'). Pasa --force si realmente quieres "
            "mezclar la semilla con datos reales, o limpia primero."
        )

    deleted = 0
    if reset:
        cur = conn.execute(
            "DELETE FROM tickets WHERE from_email = ?", (DEMO_EMAIL,)
        )
        deleted = cur.rowcount or 0

    existing = _count_demo(conn)
    if existing > 0:
        return {"created": 0, "deleted": deleted, "existing": existing}

    created = _insert_demo_batch(conn, _demo_tickets(now=now))
    return {"created": created, "deleted": deleted, "existing": 0}


def main() -> int:
    parser = argparse.ArgumentParser(description="Inserta tickets demo en la BD.")
    parser.add_argument("--db", default="data/app.db", help="Ruta a la BD SQLite.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Borra los tickets demo existentes antes de crearlos.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Permite ejecutar aunque la BD ya tenga tickets reales.",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(
            f"ERROR: BD no encontrada en {db_path.resolve()}. "
            "Ejecuta 'python scripts/init_db.py' primero.",
            file=sys.stderr,
        )
        return 1

    with get_connection(db_path) as conn:
        try:
            result = seed(conn, reset=args.reset, force=args.force)
        except SeedAbortError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    if result["existing"] > 0:
        print(
            f"OK: ya habia {result['existing']} tickets demo. "
            "Nada que hacer (usa --reset para recrear)."
        )
    else:
        msg = f"OK: creados {result['created']} tickets demo"
        if result["deleted"] > 0:
            msg += f" (borrados {result['deleted']} previos)"
        msg += "."
        print(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
