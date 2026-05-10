"""Tests del servicio de auth (``app.services.auth``).

Solo el modulo puro: hash bcrypt, cookie firmada y rate limiter. Los tests
del flujo HTTP completo viven en ``tests/test_routes_login.py``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone

import pytest

from app.db.sqlite import get_connection, init_schema
from app.models.user import User, to_db_row
from app.services.auth import (
    LoginRateLimiter,
    create_session_token,
    get_user_by_id,
    get_user_by_username,
    hash_password,
    verify_password,
    verify_session_token,
)


SECRET = "x" * 32
OTHER_SECRET = "y" * 32


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    with get_connection(":memory:") as c:
        init_schema(c)
        yield c


def _insert_user(conn: sqlite3.Connection, **overrides: object) -> int:
    user = User(
        username=overrides.get("username", "alfredo"),  # type: ignore[arg-type]
        password_hash=overrides.get("password_hash", hash_password("secreto-1234")),  # type: ignore[arg-type]
        display_name=overrides.get("display_name", "Alfredo"),  # type: ignore[arg-type]
        email=overrides.get("email", None),  # type: ignore[arg-type]
        role=overrides.get("role", "user"),  # type: ignore[arg-type]
        created_at=overrides.get(  # type: ignore[arg-type]
            "created_at", datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
        ),
    )
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, display_name, email, role, created_at) "
        "VALUES (:username, :password_hash, :display_name, :email, :role, :created_at)",
        to_db_row(user),
    )
    return cur.lastrowid  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def test_hash_password_produce_hashes_distintos_cada_vez() -> None:
    h1 = hash_password("misma-password")
    h2 = hash_password("misma-password")
    assert h1 != h2  # bcrypt usa salt distinto cada vez
    assert verify_password("misma-password", h1)
    assert verify_password("misma-password", h2)


def test_verify_password_falla_con_password_distinta() -> None:
    h = hash_password("buena")
    assert verify_password("buena", h) is True
    assert verify_password("mala", h) is False


def test_verify_password_no_propaga_excepcion_si_hash_es_basura() -> None:
    # Si la BD esta corrupta, no queremos filtrar el detalle al cliente:
    # un hash invalido se trata como "credenciales incorrectas".
    assert verify_password("cualquier", "no-es-un-hash-bcrypt") is False
    assert verify_password("cualquier", "") is False


# ---------------------------------------------------------------------------
# Sesion (itsdangerous)
# ---------------------------------------------------------------------------


def test_session_token_roundtrip() -> None:
    token = create_session_token(42, secret_key=SECRET)
    uid = verify_session_token(token, secret_key=SECRET)
    assert uid == 42


def test_session_token_falla_con_secret_distinto() -> None:
    token = create_session_token(42, secret_key=SECRET)
    assert verify_session_token(token, secret_key=OTHER_SECRET) is None


def test_session_token_falla_si_esta_corrupto() -> None:
    assert verify_session_token("basura.invented.token", secret_key=SECRET) is None
    assert verify_session_token("", secret_key=SECRET) is None


def test_session_token_expira_con_max_age_cero() -> None:
    # Forzamos expiracion: max_age=0 debe rechazar incluso un token
    # recien emitido (su edad es >= 0).
    token = create_session_token(42, secret_key=SECRET)
    # itsdangerous trata max_age <= edad como expirado solo si edad > max_age
    # (estrictamente). Para garantizar que un token "antiguo" caduca usamos
    # max_age negativo.
    assert verify_session_token(token, secret_key=SECRET, max_age=-1) is None


# ---------------------------------------------------------------------------
# Acceso a usuarios
# ---------------------------------------------------------------------------


def test_get_user_by_username(conn: sqlite3.Connection) -> None:
    user_id = _insert_user(conn, username="alfredo")
    user = get_user_by_username(conn, "alfredo")
    assert user is not None
    assert user.id == user_id
    assert user.username == "alfredo"


def test_get_user_by_username_inexistente(conn: sqlite3.Connection) -> None:
    assert get_user_by_username(conn, "no-existe") is None


def test_get_user_by_id(conn: sqlite3.Connection) -> None:
    user_id = _insert_user(conn, username="alfredo")
    user = get_user_by_id(conn, user_id)
    assert user is not None
    assert user.username == "alfredo"


def test_get_user_by_id_inexistente(conn: sqlite3.Connection) -> None:
    assert get_user_by_id(conn, 99999) is None


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


def test_rate_limiter_permite_los_primeros_intentos() -> None:
    rl = LoginRateLimiter(max_attempts=3, window_seconds=60)
    assert rl.check("1.2.3.4", "alfredo", now=0.0) == 0
    rl.record("1.2.3.4", "alfredo", now=0.0)
    assert rl.check("1.2.3.4", "alfredo", now=0.1) == 0
    rl.record("1.2.3.4", "alfredo", now=0.1)
    assert rl.check("1.2.3.4", "alfredo", now=0.2) == 0


def test_rate_limiter_bloquea_al_llegar_al_limite() -> None:
    rl = LoginRateLimiter(max_attempts=3, window_seconds=60)
    for i in range(3):
        rl.record("1.2.3.4", "alfredo", now=float(i))
    retry = rl.check("1.2.3.4", "alfredo", now=3.0)
    assert retry > 0
    assert retry <= 60


def test_rate_limiter_independencia_entre_username() -> None:
    """Mismo IP, distinto username: el bloqueo de uno no afecta al otro.

    Es la razon de modelar la clave como ``(ip, username)`` y no solo IP:
    los 3 empleados salen detras de la misma IP de oficina.
    """
    rl = LoginRateLimiter(max_attempts=3, window_seconds=60)
    for i in range(3):
        rl.record("1.2.3.4", "alfredo", now=float(i))
    assert rl.check("1.2.3.4", "alfredo", now=3.0) > 0
    assert rl.check("1.2.3.4", "marta", now=3.0) == 0


def test_rate_limiter_independencia_entre_ip() -> None:
    rl = LoginRateLimiter(max_attempts=3, window_seconds=60)
    for i in range(3):
        rl.record("1.2.3.4", "alfredo", now=float(i))
    assert rl.check("9.9.9.9", "alfredo", now=3.0) == 0


def test_rate_limiter_expira_por_ventana() -> None:
    rl = LoginRateLimiter(max_attempts=3, window_seconds=60)
    for i in range(3):
        rl.record("1.2.3.4", "alfredo", now=float(i))
    # 61s despues, los intentos viejos quedan fuera de la ventana.
    assert rl.check("1.2.3.4", "alfredo", now=61.0) == 0


def test_rate_limiter_reset_libera_al_instante() -> None:
    rl = LoginRateLimiter(max_attempts=3, window_seconds=60)
    for i in range(3):
        rl.record("1.2.3.4", "alfredo", now=float(i))
    assert rl.check("1.2.3.4", "alfredo", now=3.0) > 0
    rl.reset("1.2.3.4", "alfredo")
    assert rl.check("1.2.3.4", "alfredo", now=3.0) == 0


def test_rate_limiter_retry_after_se_reduce_con_el_tiempo() -> None:
    """``retry_after`` debe ser proporcional al tiempo que falta para el hueco."""
    rl = LoginRateLimiter(max_attempts=3, window_seconds=60)
    for i in range(3):
        rl.record("1.2.3.4", "alfredo", now=float(i))
    early = rl.check("1.2.3.4", "alfredo", now=3.0)
    later = rl.check("1.2.3.4", "alfredo", now=30.0)
    assert later < early
