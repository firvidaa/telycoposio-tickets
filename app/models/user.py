"""Modelo de usuario y conversion a/desde fila SQLite.

El campo ``role`` (v1.2) acepta ``'user'`` o ``'admin'``; en el MVP solo se
usa ``'user'``, pero se almacena desde el principio para evitar migrar la
tabla en produccion mas adelante.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


UserRole = Literal["user", "admin"]


class User(BaseModel):
    """Representacion en memoria de un usuario. Ver SPEC.md §4.2 (v1.2)."""

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    username: str
    password_hash: str
    display_name: str
    email: str | None = None
    role: UserRole = "user"
    created_at: datetime


def _iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def to_db_row(user: User) -> dict[str, Any]:
    """Diccionario para ``INSERT INTO users (...)``.

    No incluye ``id`` porque es ``AUTOINCREMENT`` y lo asigna SQLite.
    """
    return {
        "username": user.username,
        "password_hash": user.password_hash,
        "display_name": user.display_name,
        "email": user.email,
        "role": user.role,
        "created_at": _iso_utc(user.created_at),
    }


def from_db_row(row: sqlite3.Row) -> User:
    """Reconstruye un ``User`` a partir de una fila ``sqlite3.Row``."""
    return User(
        id=row["id"],
        username=row["username"],
        password_hash=row["password_hash"],
        display_name=row["display_name"],
        email=row["email"],
        role=row["role"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )
