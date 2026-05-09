"""Inicializa la BD SQLite local creando las tablas si no existen.

Idempotente: se puede ejecutar las veces que haga falta. Por defecto crea la
BD en ``data/app.db`` (relativo al cwd); usa ``--db`` para apuntar a otra ruta.

NO crea usuarios automaticamente; para anadir un usuario ejecuta despues
``scripts/add_user.py``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Permitir importar el paquete ``app`` cuando se ejecuta el script directamente.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from app.db.sqlite import get_connection, init_schema  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Inicializa la BD SQLite local.")
    parser.add_argument(
        "--db",
        default="data/app.db",
        help="Ruta a la BD SQLite (por defecto: data/app.db).",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with get_connection(db_path) as conn:
        init_schema(conn)
        conn.commit()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        tables = [r["name"] for r in rows]

    print(f"BD inicializada en: {db_path.resolve()}")
    print(f"Tablas presentes: {', '.join(tables) if tables else '(ninguna)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
