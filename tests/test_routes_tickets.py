"""Tests del flujo HTTP de listado y detalle de tickets (Paso 5b).

Usa ``TestClient`` con dependency overrides para apuntar a una BD SQLite
temporal. Cada test parte de un rate limiter limpio y, tras correr, libera
los overrides.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.db.sqlite import get_connection, init_schema
from app.main import app
from app.models.ticket import (
    TICKET_COLUMNS,
    Attachment,
    Ticket,
    TicketCategory,
    TicketChannel,
    TicketStatus,
    to_db_row,
)
from app.models.user import User
from app.models.user import to_db_row as user_to_db_row
from app.services.auth import (
    SESSION_COOKIE_NAME,
    create_session_token,
    hash_password,
    login_rate_limiter,
)
from app.web.routes import require_user_html


SECRET = "x" * 32
PASSWORD = "secreto-1234"
TEST_DUMMY_PATH = "/__test_require_user__"

# Cabeceras tipicas. ``BROWSER_HEADERS`` imita lo que envia un navegador real;
# ``JSON_HEADERS`` lo que enviaria un curl o un cliente API que quiera JSON.
BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}
JSON_HEADERS = {"Accept": "application/json"}


# ---------------------------------------------------------------------------
# Helpers de BD
# ---------------------------------------------------------------------------


def _create_user(db_path: Path, username: str = "alfredo") -> int:
    with get_connection(db_path) as conn:
        init_schema(conn)
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, display_name, email, role, created_at) "
            "VALUES (:username, :password_hash, :display_name, :email, :role, :created_at)",
            user_to_db_row(
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


def _insert_ticket(db_path: Path, ticket: Ticket) -> None:
    with get_connection(db_path) as conn:
        placeholders = ", ".join(f":{c}" for c in TICKET_COLUMNS)
        conn.execute(
            f"INSERT INTO tickets ({', '.join(TICKET_COLUMNS)}) VALUES ({placeholders})",
            to_db_row(ticket),
        )


def _ticket(
    n: int,
    *,
    status: TicketStatus = TicketStatus.NEW,
    category: TicketCategory | None = TicketCategory.SOPORTE,
    needs_review: bool = False,
    confidence: float | None = 0.9,
    minute_offset: int = 0,
    attachments: list[Attachment] | None = None,
    raw_message_id: str | None = None,
    from_email: str | None = None,
    channel: TicketChannel = TicketChannel.EMAIL,
) -> Ticket:
    base = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    created = base + timedelta(minutes=minute_offset)
    return Ticket(
        id=f"TLY-2026-{n:04d}",
        created_at=created,
        channel=channel,
        from_name=f"Cliente {n}",
        from_email=from_email if from_email is not None else f"cliente{n}@example.com",
        subject=f"Asunto numero {n}",
        body=f"Cuerpo del ticket {n}\nLinea 2.",
        status=status,
        category=category,
        category_confidence=confidence,
        category_reasoning=None if category is None else "razon",
        needs_review=needs_review,
        raw_message_id=raw_message_id,
        last_updated_at=created,
        attachments=attachments or [],
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "app.db"
    _create_user(p)
    return p


@pytest.fixture
def client(db_path: Path) -> Iterator[TestClient]:
    login_rate_limiter._attempts.clear()
    fake_settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        APP_SECRET_KEY=SECRET,
        ANTHROPIC_API_KEY="sk-ant-test",
        EMAIL_ADDRESS="test@example.com",
        EMAIL_APP_PASSWORD="abcdefghijklmnop",
        SQLITE_PATH=str(db_path),
        APP_ENV="development",
    )
    app.dependency_overrides[get_settings] = lambda: fake_settings
    try:
        with TestClient(app) as c:
            # Cookie de sesion para user_id=1 (creado por la fixture db_path).
            c.cookies.set(SESSION_COOKIE_NAME, create_session_token(1, secret_key=SECRET))
            yield c
    finally:
        app.dependency_overrides.clear()
        login_rate_limiter._attempts.clear()


@pytest.fixture
def anon_client(db_path: Path) -> Iterator[TestClient]:
    """Igual que ``client`` pero sin cookie inyectada (usuario anonimo)."""
    login_rate_limiter._attempts.clear()
    fake_settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        APP_SECRET_KEY=SECRET,
        ANTHROPIC_API_KEY="sk-ant-test",
        EMAIL_ADDRESS="test@example.com",
        EMAIL_APP_PASSWORD="abcdefghijklmnop",
        SQLITE_PATH=str(db_path),
        APP_ENV="development",
    )
    app.dependency_overrides[get_settings] = lambda: fake_settings
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()
        login_rate_limiter._attempts.clear()


@pytest.fixture
def client_with_dummy_protected_route(client: TestClient) -> Iterator[TestClient]:
    """Cliente con una ruta dummy montada en caliente que solo usa
    ``require_user_html``. El test verifica la **propiedad** de fail-secure:
    una ruta nueva que confia en la dependency sin codigo de auth manual
    sigue rechazando peticiones anonimas.
    """
    test_router = APIRouter()

    @test_router.get(TEST_DUMMY_PATH)
    def _dummy(user: User = Depends(require_user_html)) -> dict[str, str]:
        return {"hello": user.username}

    app.include_router(test_router)
    try:
        yield client
    finally:
        # Quitar la ruta dummy para no contaminar otros tests.
        app.router.routes[:] = [
            r for r in app.router.routes if getattr(r, "path", None) != TEST_DUMMY_PATH
        ]


# ---------------------------------------------------------------------------
# /tickets — sesion
# ---------------------------------------------------------------------------


def test_tickets_sin_sesion_redirige_a_login(anon_client: TestClient) -> None:
    r = anon_client.get("/tickets", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_tickets_lista_vacia_renderiza(client: TestClient) -> None:
    r = client.get("/tickets")
    assert r.status_code == 200
    assert "No hay tickets" in r.text


def test_tickets_renderiza_filas(client: TestClient, db_path: Path) -> None:
    _insert_ticket(db_path, _ticket(1, minute_offset=0))
    _insert_ticket(db_path, _ticket(2, minute_offset=10))
    r = client.get("/tickets")
    assert r.status_code == 200
    # Los IDs aparecen en el listado.
    assert "TLY-2026-0001" in r.text
    assert "TLY-2026-0002" in r.text


def test_tickets_sliding_reemite_cookie(client: TestClient) -> None:
    r = client.get("/tickets")
    assert r.status_code == 200
    assert SESSION_COOKIE_NAME in r.headers.get("set-cookie", "")


# ---------------------------------------------------------------------------
# /tickets — filtro implicito (no CLOSED por defecto)
# ---------------------------------------------------------------------------


def test_tickets_oculta_closed_por_defecto(client: TestClient, db_path: Path) -> None:
    _insert_ticket(db_path, _ticket(1, status=TicketStatus.NEW))
    _insert_ticket(db_path, _ticket(2, status=TicketStatus.CLOSED))
    r = client.get("/tickets")
    assert r.status_code == 200
    assert "TLY-2026-0001" in r.text
    assert "TLY-2026-0002" not in r.text


def test_tickets_banner_muestra_link_a_ver_todos(
    client: TestClient, db_path: Path
) -> None:
    _insert_ticket(db_path, _ticket(2, status=TicketStatus.CLOSED))
    r = client.get("/tickets")
    assert r.status_code == 200
    # El banner debe ser visible y el enlace a status=ALL debe existir.
    assert "solo tickets activos" in r.text.lower()
    assert "status=ALL" in r.text


def test_tickets_status_all_muestra_los_cerrados(
    client: TestClient, db_path: Path
) -> None:
    _insert_ticket(db_path, _ticket(1, status=TicketStatus.NEW))
    _insert_ticket(db_path, _ticket(2, status=TicketStatus.CLOSED))
    r = client.get("/tickets?status=ALL")
    assert r.status_code == 200
    assert "TLY-2026-0001" in r.text
    assert "TLY-2026-0002" in r.text
    assert "solo tickets activos" not in r.text.lower()


# ---------------------------------------------------------------------------
# /tickets — filtros explicitos
# ---------------------------------------------------------------------------


def test_filtro_status_explicito(client: TestClient, db_path: Path) -> None:
    _insert_ticket(db_path, _ticket(1, status=TicketStatus.NEW))
    _insert_ticket(db_path, _ticket(2, status=TicketStatus.IN_PROGRESS))
    r = client.get("/tickets?status=NEW")
    assert "TLY-2026-0001" in r.text
    assert "TLY-2026-0002" not in r.text


def test_filtro_category(client: TestClient, db_path: Path) -> None:
    _insert_ticket(db_path, _ticket(1, category=TicketCategory.SOPORTE))
    _insert_ticket(db_path, _ticket(2, category=TicketCategory.COMERCIAL))
    r = client.get("/tickets?category=COMERCIAL")
    assert "TLY-2026-0001" not in r.text
    assert "TLY-2026-0002" in r.text


def test_filtro_uncategorized(client: TestClient, db_path: Path) -> None:
    _insert_ticket(db_path, _ticket(1, category=TicketCategory.SOPORTE))
    _insert_ticket(
        db_path, _ticket(2, category=None, confidence=None, needs_review=True)
    )
    r = client.get("/tickets?category=UNCATEGORIZED")
    assert "TLY-2026-0001" not in r.text
    assert "TLY-2026-0002" in r.text


def test_filtro_needs_review_true(client: TestClient, db_path: Path) -> None:
    _insert_ticket(db_path, _ticket(1, needs_review=False))
    _insert_ticket(db_path, _ticket(2, needs_review=True))
    r = client.get("/tickets?needs_review=true")
    assert "TLY-2026-0001" not in r.text
    assert "TLY-2026-0002" in r.text


def test_filtro_status_invalido_devuelve_422(client: TestClient) -> None:
    r = client.get("/tickets?status=INVENTADO")
    assert r.status_code == 422


def test_filtro_category_invalida_devuelve_422(client: TestClient) -> None:
    r = client.get("/tickets?category=BASURA")
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# /tickets — paginacion
# ---------------------------------------------------------------------------


def test_paginacion_genera_link_a_siguiente(client: TestClient, db_path: Path) -> None:
    # Insertamos 51 tickets para forzar has_next.
    for i in range(1, 52):
        _insert_ticket(
            db_path,
            _ticket(i, status=TicketStatus.NEW, minute_offset=i),
        )
    r = client.get("/tickets")
    assert r.status_code == 200
    # offset=50 debe aparecer en el href de "Siguientes".
    assert "offset=50" in r.text
    assert "Siguientes" in r.text


def test_paginacion_segunda_pagina(client: TestClient, db_path: Path) -> None:
    for i in range(1, 52):
        _insert_ticket(
            db_path,
            _ticket(i, status=TicketStatus.NEW, minute_offset=i),
        )
    r = client.get("/tickets?offset=50")
    assert r.status_code == 200
    assert "Anteriores" in r.text
    # Solo queda 1 ticket en la pagina 2 (el mas viejo).
    assert "TLY-2026-0001" in r.text


def test_paginacion_offset_negativo_devuelve_422(client: TestClient) -> None:
    r = client.get("/tickets?offset=-1")
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# /tickets/{id}
# ---------------------------------------------------------------------------


def test_detalle_sin_sesion_redirige(anon_client: TestClient) -> None:
    r = anon_client.get("/tickets/TLY-2026-0001", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_detalle_existente(client: TestClient, db_path: Path) -> None:
    _insert_ticket(
        db_path,
        _ticket(
            1,
            attachments=[Attachment(name="captura.png", url="https://x.test/a.png")],
        ),
    )
    r = client.get("/tickets/TLY-2026-0001")
    assert r.status_code == 200
    assert "TLY-2026-0001" in r.text
    assert "Asunto numero 1" in r.text
    assert "Cuerpo del ticket 1" in r.text
    assert "captura.png" in r.text
    assert "https://x.test/a.png" in r.text


def test_detalle_adjunto_sin_url_muestra_solo_nombre_y_tamano(
    client: TestClient, db_path: Path
) -> None:
    """Caso MVP v1.3: adjunto sin url; render = nombre + tamanyo, sin <a href>."""
    _insert_ticket(
        db_path,
        _ticket(
            1,
            attachments=[Attachment(name="captura.png", size_bytes=58_400)],
        ),
    )
    r = client.get("/tickets/TLY-2026-0001")
    assert r.status_code == 200
    assert "captura.png" in r.text
    # Tamanyo formateado por filter human_size: 58400 bytes ≈ 57.0 KB.
    assert "57.0 KB" in r.text
    # No debe haber link al adjunto cuando url es None.
    assert "href=\"None\"" not in r.text
    assert "captura.png</a>" not in r.text  # no esta envuelto en <a>...</a>


def test_detalle_inexistente_404(client: TestClient) -> None:
    r = client.get("/tickets/TLY-2026-9999")
    assert r.status_code == 404


def test_detalle_formato_invalido_404(client: TestClient) -> None:
    """IDs que no encajan con TLY-YYYY-NNNN devuelven 404 sin tocar BD."""
    r = client.get("/tickets/no-formato-valido")
    assert r.status_code == 404


def test_detalle_sliding_reemite_cookie(client: TestClient, db_path: Path) -> None:
    _insert_ticket(db_path, _ticket(1))
    r = client.get("/tickets/TLY-2026-0001")
    assert r.status_code == 200
    assert SESSION_COOKIE_NAME in r.headers.get("set-cookie", "")


# ---------------------------------------------------------------------------
# Fail-secure de require_user_html como invariante
# ---------------------------------------------------------------------------


def test_require_user_html_es_fail_secure_para_rutas_nuevas(
    client_with_dummy_protected_route: TestClient,
) -> None:
    """Una ruta recien montada con ``require_user_html`` y **sin** codigo de
    auth manual sigue rechazando peticiones sin sesion. Esta es la propiedad
    que justifica el patrón: cero margen para olvidos en el futuro."""
    client_with_dummy_protected_route.cookies.clear()
    r = client_with_dummy_protected_route.get(TEST_DUMMY_PATH, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_require_user_html_deja_pasar_con_sesion(
    client_with_dummy_protected_route: TestClient,
) -> None:
    r = client_with_dummy_protected_route.get(TEST_DUMMY_PATH)
    assert r.status_code == 200
    assert r.json() == {"hello": "alfredo"}


def test_require_user_html_limpia_cookie_stale(
    client_with_dummy_protected_route: TestClient,
) -> None:
    """Si la cookie es invalida (firma mala), el handler de la excepcion la
    borra al redirigir a /login."""
    client_with_dummy_protected_route.cookies.set(SESSION_COOKIE_NAME, "basura.no-firmada")
    r = client_with_dummy_protected_route.get(TEST_DUMMY_PATH, follow_redirects=False)
    assert r.status_code == 303
    set_cookie = r.headers.get("set-cookie", "")
    assert SESSION_COOKIE_NAME in set_cookie
    assert "Max-Age=0" in set_cookie or "expires=" in set_cookie.lower()


# ---------------------------------------------------------------------------
# Paginas de error 404 / 422: HTML para navegador, JSON para cliente API
# ---------------------------------------------------------------------------


def test_404_navegador_recibe_html_ticket_inexistente(
    client: TestClient,
) -> None:
    """ID con formato valido pero no presente en BD."""
    r = client.get("/tickets/TLY-2099-0001", headers=BROWSER_HEADERS)
    assert r.status_code == 404
    assert "text/html" in r.headers["content-type"]
    assert "Ticket no encontrado" in r.text
    assert "Volver al listado" in r.text


def test_404_navegador_recibe_html_formato_invalido(
    client: TestClient,
) -> None:
    """ID que no encaja con TLY-YYYY-NNNN: 404 sin tocar BD, en HTML."""
    r = client.get("/tickets/no-formato", headers=BROWSER_HEADERS)
    assert r.status_code == 404
    assert "text/html" in r.headers["content-type"]
    assert "Ticket no encontrado" in r.text


def test_404_navegador_recibe_html_path_arbitrario(
    client: TestClient,
) -> None:
    """Una URL que no existe en absoluto tambien renderiza la pagina HTML."""
    r = client.get("/no-existe", headers=BROWSER_HEADERS)
    assert r.status_code == 404
    assert "text/html" in r.headers["content-type"]
    assert "Pagina no encontrada" in r.text


def test_404_cliente_api_recibe_json(client: TestClient) -> None:
    """Con ``Accept: application/json`` el cuerpo sigue siendo JSON estructurado."""
    r = client.get("/tickets/TLY-2099-0001", headers=JSON_HEADERS)
    assert r.status_code == 404
    assert "application/json" in r.headers["content-type"]
    assert r.json() == {"detail": "Not Found"}


def test_422_navegador_recibe_html(client: TestClient) -> None:
    r = client.get("/tickets?status=BASURA", headers=BROWSER_HEADERS)
    assert r.status_code == 422
    assert "text/html" in r.headers["content-type"]
    assert "Filtros invalidos" in r.text
    assert "Volver al listado" in r.text


def test_422_cliente_api_recibe_json(client: TestClient) -> None:
    r = client.get("/tickets?status=BASURA", headers=JSON_HEADERS)
    assert r.status_code == 422
    assert "application/json" in r.headers["content-type"]
    body = r.json()
    assert "detail" in body
    # FastAPI emite una lista de errores estructurados (igual que el handler default).
    assert isinstance(body["detail"], list)
    assert any("status" in str(err.get("loc", [])) for err in body["detail"])


# ---------------------------------------------------------------------------
# POST /tickets/{id}/reply (Paso 10)
# ---------------------------------------------------------------------------


class _FakeSender:
    """Doble del EmailSender para tests de ruta. Programable por test."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.message_id = "<reply-from-test@x>"
        self.error: Exception | None = None

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        in_reply_to: str | None = None,
    ) -> str:
        self.calls.append(
            {"to": to, "subject": subject, "body": body, "in_reply_to": in_reply_to}
        )
        if self.error is not None:
            raise self.error
        return self.message_id


@pytest.fixture
def fake_sender(client: TestClient) -> Iterator[_FakeSender]:
    """Sobrescribe ``get_email_sender`` con un doble. Devuelve el doble."""
    from app.web.routes import get_email_sender

    sender = _FakeSender()
    app.dependency_overrides[get_email_sender] = lambda: sender
    try:
        yield sender
    finally:
        # Quitar solo este override; los demas los limpia la fixture client.
        app.dependency_overrides.pop(get_email_sender, None)


def _count_replies(db_path: Path, ticket_id: str) -> int:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM ticket_replies WHERE ticket_id = ?",
            (ticket_id,),
        ).fetchone()
    return int(row["n"])


def test_reply_sin_sesion_redirige_a_login(anon_client: TestClient) -> None:
    r = anon_client.post(
        "/tickets/TLY-2026-0001/reply",
        data={"body": "x", "new_status": "WAITING"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_reply_caso_feliz_303_persiste_y_envia(
    client: TestClient, fake_sender: _FakeSender, db_path: Path
) -> None:
    _insert_ticket(
        db_path,
        _ticket(1, raw_message_id="<original@cli>"),
    )

    r = client.post(
        "/tickets/TLY-2026-0001/reply",
        data={"body": "Hola, te respondemos.", "new_status": "WAITING"},
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert r.headers["location"] == "/tickets/TLY-2026-0001"
    # SMTP invocado con los args correctos.
    assert len(fake_sender.calls) == 1
    call = fake_sender.calls[0]
    assert call["to"] == "cliente1@example.com"
    assert call["subject"] == "Re: Asunto numero 1"
    assert call["in_reply_to"] == "<original@cli>"
    # Reply persistida.
    assert _count_replies(db_path, "TLY-2026-0001") == 1


def test_reply_cambia_estado_si_se_pide(
    client: TestClient, fake_sender: _FakeSender, db_path: Path
) -> None:
    _insert_ticket(
        db_path,
        _ticket(1, status=TicketStatus.NEW, raw_message_id="<o@c>"),
    )

    client.post(
        "/tickets/TLY-2026-0001/reply",
        data={"body": "x", "new_status": "CLOSED"},
        follow_redirects=False,
    )

    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM tickets WHERE id = 'TLY-2026-0001'"
        ).fetchone()
    assert row["status"] == "CLOSED"


def test_reply_no_change_deja_status_intacto(
    client: TestClient, fake_sender: _FakeSender, db_path: Path
) -> None:
    _insert_ticket(
        db_path,
        _ticket(1, status=TicketStatus.IN_PROGRESS, raw_message_id="<o@c>"),
    )

    client.post(
        "/tickets/TLY-2026-0001/reply",
        data={"body": "x", "new_status": "NO_CHANGE"},
        follow_redirects=False,
    )

    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM tickets WHERE id = 'TLY-2026-0001'"
        ).fetchone()
    assert row["status"] == "IN_PROGRESS"


def test_reply_body_vacio_400_no_persiste_ni_envia(
    client: TestClient, fake_sender: _FakeSender, db_path: Path
) -> None:
    _insert_ticket(db_path, _ticket(1, raw_message_id="<o@c>"))

    r = client.post(
        "/tickets/TLY-2026-0001/reply",
        data={"body": "   \n  ", "new_status": "WAITING"},
    )

    assert r.status_code == 400
    # El mensaje de error del servicio aparece en la pagina.
    assert "body vacio" in r.text
    assert fake_sender.calls == []
    assert _count_replies(db_path, "TLY-2026-0001") == 0


def test_reply_ticket_sin_raw_message_id_400(
    client: TestClient, fake_sender: _FakeSender, db_path: Path
) -> None:
    # Demo ticket: sin raw_message_id.
    _insert_ticket(db_path, _ticket(1, raw_message_id=None))

    r = client.post(
        "/tickets/TLY-2026-0001/reply",
        data={"body": "Hola", "new_status": "WAITING"},
    )

    assert r.status_code == 400
    assert "raw_message_id" in r.text
    assert fake_sender.calls == []
    assert _count_replies(db_path, "TLY-2026-0001") == 0


def test_reply_status_invalido_400(
    client: TestClient, fake_sender: _FakeSender, db_path: Path
) -> None:
    _insert_ticket(db_path, _ticket(1, raw_message_id="<o@c>"))

    r = client.post(
        "/tickets/TLY-2026-0001/reply",
        data={"body": "Hola", "new_status": "INVENTADO"},
    )

    assert r.status_code == 400
    assert "status invalido" in r.text
    assert fake_sender.calls == []
    assert _count_replies(db_path, "TLY-2026-0001") == 0


def test_reply_ticket_inexistente_404(
    client: TestClient, fake_sender: _FakeSender
) -> None:
    r = client.post(
        "/tickets/TLY-2099-0001/reply",
        data={"body": "Hola", "new_status": "WAITING"},
    )
    assert r.status_code == 404


def test_reply_smtp_falla_502_no_persiste(
    client: TestClient, fake_sender: _FakeSender, db_path: Path
) -> None:
    _insert_ticket(db_path, _ticket(1, raw_message_id="<o@c>"))
    fake_sender.error = RuntimeError("smtp caido")

    r = client.post(
        "/tickets/TLY-2026-0001/reply",
        data={"body": "Hola", "new_status": "WAITING"},
    )

    assert r.status_code == 502
    assert "No se pudo enviar" in r.text
    assert _count_replies(db_path, "TLY-2026-0001") == 0


# ---------------------------------------------------------------------------
# GET /tickets/{id} con historial y form (Paso 10)
# ---------------------------------------------------------------------------


def test_detalle_muestra_form_de_respuesta_si_es_respondible(
    client: TestClient, db_path: Path
) -> None:
    _insert_ticket(db_path, _ticket(1, raw_message_id="<o@c>"))
    r = client.get("/tickets/TLY-2026-0001")
    assert r.status_code == 200
    assert "Responder al cliente" in r.text
    # textarea del form presente.
    assert 'name="body"' in r.text
    # Subject preview incluye Re:.
    assert "Re: Asunto numero 1" in r.text


def test_detalle_oculta_form_si_ticket_sin_raw_message_id(
    client: TestClient, db_path: Path
) -> None:
    """Ticket demo o creado a mano: el form se oculta y se muestra el motivo."""
    _insert_ticket(db_path, _ticket(1, raw_message_id=None))
    r = client.get("/tickets/TLY-2026-0001")
    assert r.status_code == 200
    assert "no es respondible" in r.text.lower()
    # No hay form ni textarea de respuesta.
    assert 'name="body"' not in r.text


def test_detalle_oculta_form_si_canal_no_email(
    client: TestClient, db_path: Path
) -> None:
    _insert_ticket(
        db_path,
        _ticket(
            1,
            channel=TicketChannel.WHATSAPP,
            from_email=None,
            raw_message_id=None,
        ),
    )
    r = client.get("/tickets/TLY-2026-0001")
    assert r.status_code == 200
    assert "no es respondible" in r.text.lower()
    assert "whatsapp" in r.text.lower()


def test_detalle_muestra_historial_de_respuestas(
    client: TestClient, fake_sender: _FakeSender, db_path: Path
) -> None:
    """Tras enviar dos respuestas el GET debe renderizarlas en orden ASC."""
    _insert_ticket(db_path, _ticket(1, raw_message_id="<o@c>"))

    fake_sender.message_id = "<reply-1@x>"
    client.post(
        "/tickets/TLY-2026-0001/reply",
        data={"body": "Primera.", "new_status": "WAITING"},
        follow_redirects=False,
    )
    fake_sender.message_id = "<reply-2@x>"
    client.post(
        "/tickets/TLY-2026-0001/reply",
        data={"body": "Segunda.", "new_status": "NO_CHANGE"},
        follow_redirects=False,
    )

    r = client.get("/tickets/TLY-2026-0001")
    assert r.status_code == 200
    assert "Historial de respuestas (2)" in r.text
    # Aparecen ambas, y la primera aparece antes que la segunda en el HTML.
    assert "Primera." in r.text
    assert "Segunda." in r.text
    assert r.text.index("Primera.") < r.text.index("Segunda.")
