"""Anade un usuario a la BD SQLite local.

Pide la contrasena por stdin (sin eco) y la almacena como hash bcrypt.
Falla si la BD no existe (ejecuta ``scripts/init_db.py`` primero) o si el
``username`` ya esta en uso.

Uso:
    python scripts/add_user.py --username alfredo --display-name "Alfredo"
    python scripts/add_user.py --username alfredo --display-name "Alfredo" --role admin
"""

from __future__ import annotations

import argparse
import getpass
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import bcrypt

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from app.db.sqlite import get_connection  # noqa: E402

MIN_PASSWORD_LEN = 8


def _read_password() -> str | None:
    """Pide la contrasena dos veces y la devuelve, o ``None`` si no es valida."""
    pw1 = getpass.getpass("Contrasena: ")
    pw2 = getpass.getpass("Repite contrasena: ")
    if pw1 != pw2:
        print("ERROR: las contrasenas no coinciden.", file=sys.stderr)
        return None
    if len(pw1) < MIN_PASSWORD_LEN:
        print(
            f"ERROR: la contrasena debe tener al menos {MIN_PASSWORD_LEN} caracteres.",
            file=sys.stderr,
        )
        return None
    return pw1


def main() -> int:
    parser = argparse.ArgumentParser(description="Anade un usuario a la BD local.")
    parser.add_argument("--db", default="data/app.db", help="Ruta a la BD SQLite.")
    parser.add_argument("--username", required=True, help="Login (debe ser unico).")
    parser.add_argument("--display-name", required=True, help="Nombre visible.")
    parser.add_argument("--email", default=None, help="Email (opcional).")
    parser.add_argument(
        "--role",
        default="user",
        choices=("user", "admin"),
        help="Rol (por defecto: user).",
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

    password = _read_password()
    if password is None:
        return 1

    password_hash = bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")

    created_at = datetime.now(timezone.utc).isoformat()

    with get_connection(db_path) as conn:
        try:
            conn.execute(
                "INSERT INTO users "
                "(username, password_hash, display_name, email, role, created_at) "
                "VALUES (:username, :password_hash, :display_name, :email, :role, :created_at)",
                {
                    "username": args.username,
                    "password_hash": password_hash,
                    "display_name": args.display_name,
                    "email": args.email,
                    "role": args.role,
                    "created_at": created_at,
                },
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            print(f"ERROR: no se pudo crear el usuario: {exc}", file=sys.stderr)
            return 1

    print(f"Usuario {args.username!r} creado correctamente (rol: {args.role}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
