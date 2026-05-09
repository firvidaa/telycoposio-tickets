"""Capa de acceso a SQLite local.

Proporciona:
- ``get_connection``: context manager que abre una conexion SQLite con
  ``row_factory = sqlite3.Row``, ``foreign_keys = ON`` e ``isolation_level = None``
  (autocommit). Las transacciones se gestionan explicitamente con ``transaction``.
- ``transaction``: context manager para envolver SELECT+INSERT en una transaccion
  con el modo deseado (DEFERRED, IMMEDIATE, EXCLUSIVE). Hace COMMIT al salir
  limpio o ROLLBACK si se levanta una excepcion.
- ``init_schema``: ejecuta el archivo ``schema.sql`` de esta misma carpeta.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


@contextmanager
def get_connection(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Abre una conexion a la BD en ``db_path`` y la cierra al salir del bloque.

    Configuracion:
    - ``row_factory = sqlite3.Row`` para acceder a las columnas por nombre.
    - ``isolation_level = None`` (autocommit). Cada ``execute`` se confirma
      al instante salvo que estemos dentro de un bloque ``transaction(...)``.
      Esto permite controlar con precision cuando se inicia una transaccion
      ``IMMEDIATE`` (necesario en ``ticket_service`` para que SELECT+INSERT
      sean atomicos).
    - ``PRAGMA foreign_keys = ON`` por si en el futuro definimos FKs.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(
    conn: sqlite3.Connection, mode: str = "DEFERRED"
) -> Iterator[None]:
    """Context manager para una transaccion explicita.

    ``mode`` puede ser ``"DEFERRED"`` (default de SQLite), ``"IMMEDIATE"``
    o ``"EXCLUSIVE"``. ``IMMEDIATE`` adquiere el RESERVED lock al instante,
    asi que un ``SELECT`` posterior dentro del mismo bloque no puede ser
    pisado por otro writer.

    Requiere que la conexion este en autocommit (``isolation_level = None``),
    cosa que ``get_connection`` ya garantiza.
    """
    if mode not in ("DEFERRED", "IMMEDIATE", "EXCLUSIVE"):
        raise ValueError(f"modo de transaccion invalido: {mode!r}")
    conn.execute(f"BEGIN {mode}")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def init_schema(conn: sqlite3.Connection) -> None:
    """Aplica ``schema.sql`` sobre la conexion dada.

    El esquema usa ``CREATE TABLE IF NOT EXISTS`` y ``CREATE INDEX IF NOT EXISTS``,
    asi que es seguro ejecutarlo varias veces.
    """
    sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(sql)
