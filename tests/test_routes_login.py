"""Tests del flujo HTTP de login/logout (``app.web.routes``).

Usa ``TestClient`` con dependency overrides para apuntar a una BD SQLite
temporal y unas settings con ``APP_SECRET_KEY`` controlada. Cada test parte
de un rate limiter limpio (la instancia es global en el modulo).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.db.sqlite import get_connection, init_schema
from app.main import app
from app.models.user import User, to_db_row
from app.services.auth import (
    SESSION_COOKIE_NAME,
    create_session_token,
    hash_password,
    login_rate_limiter,
)


SECRET = "x" * 32
PASSWORD = "secreto-1234"


def _create_user(db_path: Path, username: str = "alfredo") -> int:
    with get_connection(db_path) as conn:
        init_schema(conn)
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, display_name, email, role, created_at) "
            "VALUES (:username, :password_hash, :display_name, :email, :role, :created_at)",
            to_db_row(
                User(
                    username=username,
                    password_hash=hash_password(PASSWORD),
                    display_name=username.capitalize(),
                    role="user",
                    created_at=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
                )
            ),
        )
        return cur.lastrowid  # type: ignore[return-value]


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """Cliente FastAPI con BD temp, settings controladas y limiter limpio."""
    login_rate_limiter._attempts.clear()
    db_path = tmp_path / "app.db"
    _create_user(db_path)

    fake_settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        APP_SECRET_KEY=SECRET,
        ANTHROPIC_API_KEY="sk-ant-test",
        EMAIL_ADDRESS="test@example.com",
        EMAIL_APP_PASSWORD="abcdefghijklmnop",
        SQLITE_PATH=str(db_path),
        APP_ENV="development",  # cookie sin Secure para TestClient
    )
    app.dependency_overrides[get_settings] = lambda: fake_settings
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()
        login_rate_limiter._attempts.clear()


# ---------------------------------------------------------------------------
# /
# ---------------------------------------------------------------------------


def test_root_sin_sesion_redirige_a_login(client: TestClient) -> None:
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_root_con_sesion_redirige_a_tickets(client: TestClient) -> None:
    # Inyectamos una cookie valida sin pasar por POST /login.
    token = create_session_token(1, secret_key=SECRET)
    client.cookies.set(SESSION_COOKIE_NAME, token)
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/tickets"


# ---------------------------------------------------------------------------
# GET /login
# ---------------------------------------------------------------------------


def test_get_login_muestra_formulario(client: TestClient) -> None:
    r = client.get("/login")
    assert r.status_code == 200
    assert "<form" in r.text
    assert 'name="username"' in r.text
    assert 'name="password"' in r.text


def test_get_login_si_ya_autenticado_redirige(client: TestClient) -> None:
    token = create_session_token(1, secret_key=SECRET)
    client.cookies.set(SESSION_COOKIE_NAME, token)
    r = client.get("/login", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/tickets"


# ---------------------------------------------------------------------------
# POST /login
# ---------------------------------------------------------------------------


def test_post_login_correcto_pone_cookie_y_redirige(client: TestClient) -> None:
    r = client.post(
        "/login",
        data={"username": "alfredo", "password": PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/tickets"
    # La cookie de sesion viaja en Set-Cookie.
    set_cookie = r.headers.get("set-cookie", "")
    assert SESSION_COOKIE_NAME in set_cookie


def test_post_login_password_incorrecta_devuelve_401(client: TestClient) -> None:
    r = client.post(
        "/login",
        data={"username": "alfredo", "password": "mala"},
        follow_redirects=False,
    )
    assert r.status_code == 401
    assert "incorrectos" in r.text.lower()
    assert SESSION_COOKIE_NAME not in r.headers.get("set-cookie", "")


def test_post_login_usuario_inexistente_tambien_401(client: TestClient) -> None:
    """Mismo error que password mala: no filtramos si el user existe o no."""
    r = client.post(
        "/login",
        data={"username": "no-existe", "password": "lo-que-sea"},
        follow_redirects=False,
    )
    assert r.status_code == 401


def test_post_login_rate_limit_se_dispara_al_sexto(client: TestClient) -> None:
    for _ in range(5):
        r = client.post(
            "/login",
            data={"username": "alfredo", "password": "mala"},
            follow_redirects=False,
        )
        assert r.status_code == 401

    r = client.post(
        "/login",
        data={"username": "alfredo", "password": "mala"},
        follow_redirects=False,
    )
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    assert int(r.headers["Retry-After"]) > 0


def test_post_login_rate_limit_es_por_username(client: TestClient) -> None:
    """Bloquear a 'alfredo' no debe bloquear a 'marta' desde la misma IP."""
    for _ in range(5):
        client.post(
            "/login",
            data={"username": "alfredo", "password": "mala"},
            follow_redirects=False,
        )
    # 'marta' no existe en BD, pero el rate limiter no lo sabe (no llega a BD
    # cuando el contador de 'marta' aun esta a 0). Debe responder 401, no 429.
    r = client.post(
        "/login",
        data={"username": "marta", "password": "lo-que-sea"},
        follow_redirects=False,
    )
    assert r.status_code == 401


def test_post_login_correcto_resetea_rate_limit(client: TestClient) -> None:
    """Tras un login OK el contador del (ip, username) debe quedar a 0."""
    for _ in range(3):
        client.post(
            "/login",
            data={"username": "alfredo", "password": "mala"},
            follow_redirects=False,
        )
    # Login correcto: resetea.
    r = client.post(
        "/login",
        data={"username": "alfredo", "password": PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # Ahora vuelve a haber 5 intentos fallidos disponibles.
    for _ in range(5):
        # Limpiamos cookie para forzar a entrar por la rama de rate limit / 401.
        client.cookies.clear()
        r = client.post(
            "/login",
            data={"username": "alfredo", "password": "mala"},
            follow_redirects=False,
        )
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# /logout
# ---------------------------------------------------------------------------


def test_logout_borra_cookie_y_redirige(client: TestClient) -> None:
    token = create_session_token(1, secret_key=SECRET)
    client.cookies.set(SESSION_COOKIE_NAME, token)
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    set_cookie = r.headers.get("set-cookie", "")
    # delete_cookie escribe Max-Age=0 (o expires en el pasado).
    assert SESSION_COOKIE_NAME in set_cookie
    assert "Max-Age=0" in set_cookie or "expires=" in set_cookie.lower()


# ---------------------------------------------------------------------------
# /tickets (placeholder protegido)
# ---------------------------------------------------------------------------


def test_tickets_sin_sesion_redirige_a_login(client: TestClient) -> None:
    r = client.get("/tickets", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_tickets_con_sesion_renderiza_placeholder(client: TestClient) -> None:
    token = create_session_token(1, secret_key=SECRET)
    client.cookies.set(SESSION_COOKIE_NAME, token)
    r = client.get("/tickets")
    assert r.status_code == 200
    assert "Alfredo" in r.text  # display_name del usuario en la cabecera
    assert "Cerrar sesion" in r.text


def test_cookie_firmada_con_secret_distinto_no_autentica(client: TestClient) -> None:
    bad_token = create_session_token(1, secret_key="z" * 32)
    client.cookies.set(SESSION_COOKIE_NAME, bad_token)
    r = client.get("/tickets", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_cookie_de_usuario_borrado_se_limpia(client: TestClient, tmp_path: Path) -> None:
    """Si el ``user_id`` de la cookie ya no existe en BD, la app limpia la cookie."""
    # Token para un user_id que no existe (solo hemos creado uno con id=1).
    token = create_session_token(9999, secret_key=SECRET)
    client.cookies.set(SESSION_COOKIE_NAME, token)
    r = client.get("/tickets", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# Sliding renewal: cada request autenticada re-emite cookie con timestamp nuevo
# ---------------------------------------------------------------------------


def test_sliding_renewal_reemite_cookie_en_cada_request(client: TestClient) -> None:
    token = create_session_token(1, secret_key=SECRET)
    client.cookies.set(SESSION_COOKIE_NAME, token)
    r = client.get("/tickets")
    assert r.status_code == 200
    # En cada hit a una ruta que pasa por current_user con sesion valida,
    # set_cookie vuelve a aparecer en la respuesta.
    assert SESSION_COOKIE_NAME in r.headers.get("set-cookie", "")
