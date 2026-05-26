"""Rutas web: login, logout, listado y detalle de tickets.

Dos patrones de proteccion:

- ``current_user`` (dependency read-only): para rutas que pueden ir con o sin
  sesion (``/`` y ``GET /login`` deciden ellas mismas que hacer en cada caso).
- ``require_user_html`` (dependency estricta): para rutas que solo tienen
  sentido logueado (``/tickets``, ``/tickets/{id}``). Si no hay sesion lanza
  ``SessionRequiredError``, que un exception handler global traduce a 303
  hacia ``/login`` y limpia la cookie stale. **Fail-secure por defecto:**
  si en el futuro alguien anyade una ruta nueva con esta dependency y
  olvida un ``if user is None`` en el cuerpo, la app sigue rechazando
  peticiones anonimas.

Sliding renewal: cada handler protegido usa ``_render_with_session`` o
re-emite la cookie en su redirect. La dependency no muta la respuesta
porque cuando un handler devuelve ``TemplateResponse`` los headers
inyectados desde ``Response`` no se propagan.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import Settings, get_settings
from app.db.sqlite import get_connection
from app.models.ticket import TicketCategory, TicketStatus
from app.models.user import User
from app.services.auth import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    create_session_token,
    get_user_by_id,
    get_user_by_username,
    login_rate_limiter,
    verify_password,
    verify_session_token,
)
from app.services.email_client import EmailClient
from app.services.reply_service import EmailSender, send_reply
from app.services.ticket_service import get_replies, get_ticket, list_tickets


logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_LOCAL_TZ = ZoneInfo("Europe/Madrid")


def _format_madrid(dt: datetime | None, fmt: str = "%d/%m/%Y %H:%M") -> str:
    """Filtro Jinja: convierte UTC a Europe/Madrid y formatea.

    SPEC §4: la conversion a hora local solo se hace al renderizar.
    """
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_LOCAL_TZ).strftime(fmt)


_STATUS_LABELS: dict[str, str] = {
    "NEW": "Nuevo",
    "IN_PROGRESS": "En curso",
    "WAITING": "En espera",
    "CLOSED": "Cerrado",
}
_CATEGORY_LABELS: dict[str, str] = {
    "ADMINISTRATIVO": "Administrativo",
    "COMERCIAL": "Comercial",
    "SOPORTE": "Soporte",
}


def _status_label(value: object) -> str:
    return _STATUS_LABELS.get(str(value), str(value))


def _category_label(value: object) -> str:
    if value is None or value == "":
        return "Sin clasificar"
    return _CATEGORY_LABELS.get(str(value), str(value))


def _human_size(value: object) -> str:
    """Formatea ``size_bytes`` como '12 B' / '3.4 KB' / '1.2 MB'. ``None`` → ''."""
    if value is None:
        return ""
    try:
        b = int(value)
    except (TypeError, ValueError):
        return ""
    if b < 1024:
        return f"{b} B"
    if b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    return f"{b / (1024 * 1024):.1f} MB"


templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
templates.env.filters["madrid"] = _format_madrid
templates.env.filters["status_label"] = _status_label
templates.env.filters["category_label"] = _category_label
templates.env.filters["human_size"] = _human_size

router = APIRouter()


# ---------------------------------------------------------------------------
# Auth: dependencies y exception handler
# ---------------------------------------------------------------------------


class SessionRequiredError(Exception):
    """Marca que una ruta protegida no recibio una sesion valida.

    El handler global registrado en ``app.main`` la traduce en un 303 hacia
    ``/login`` y limpia la cookie del cliente si la habia.
    """


def get_db_connection(
    settings: Settings = Depends(get_settings),
) -> Iterator[sqlite3.Connection]:
    """Abre una conexion SQLite por request y la cierra al terminar."""
    with get_connection(settings.SQLITE_PATH) as conn:
        yield conn


def get_email_sender(settings: Settings = Depends(get_settings)) -> EmailSender:
    """Construye el cliente SMTP. ``EmailClient`` es lazy: no abre red
    en el constructor. Los tests sobreescriben esta dependency.
    """
    return EmailClient(settings=settings)


def current_user(
    request: Request,
    settings: Settings = Depends(get_settings),
    conn: sqlite3.Connection = Depends(get_db_connection),
) -> User | None:
    """Devuelve el usuario logueado o ``None``. **No** muta la respuesta.

    La renovacion de la cookie (sliding) la hace cada handler con
    ``_render_with_session``; esta dependency solo lee.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    user_id = verify_session_token(token, secret_key=settings.APP_SECRET_KEY)
    if user_id is None:
        return None
    return get_user_by_id(conn, user_id)


def require_user_html(user: User | None = Depends(current_user)) -> User:
    """Dependency estricta: lanza ``SessionRequiredError`` si no hay sesion.

    Pensada para rutas HTML protegidas. El handler global se encarga del
    redirect a ``/login`` y de limpiar la cookie stale.
    """
    if user is None:
        raise SessionRequiredError()
    return user


def _wants_html(request: Request) -> bool:
    """Indica si el cliente prefiere HTML (navegador) sobre JSON (cliente API).

    Regla: HTML solo si el cliente lo declara explicitamente en ``Accept``.
    Sin ``Accept``, ``Accept: */*`` o ``Accept: application/json`` → JSON.
    Asi un script de monitorizacion (curl sin Accept, o cliente que pide
    JSON) sigue recibiendo respuestas estructuradas el dia que anyadamos
    endpoints REST propios.
    """
    accept = request.headers.get("accept", "")
    return "text/html" in accept


def not_found_html(request: Request) -> Response:
    """Render HTML del 404. Distingue entre 'ticket' y 'pagina' segun el path."""
    is_ticket = request.url.path.startswith("/tickets/")
    title = "Ticket no encontrado" if is_ticket else "Pagina no encontrada"
    if is_ticket:
        message = "El ticket que buscas no existe o el ID no tiene un formato valido."
    else:
        message = "La direccion que has visitado no existe en esta aplicacion."
    return templates.TemplateResponse(
        request=request,
        name="error_404.html",
        context={"title": title, "message": message},
        status_code=status.HTTP_404_NOT_FOUND,
    )


def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> Response:
    """Captura ``HTTPException``s de Starlette/FastAPI.

    Para 404, renderiza HTML cuando el cliente acepta ``text/html``; en
    cualquier otro caso (otros statuses, cliente API), JSON con la misma
    forma que el handler default de FastAPI.
    """
    if exc.status_code == status.HTTP_404_NOT_FOUND and _wants_html(request):
        return not_found_html(request)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=getattr(exc, "headers", None),
    )


def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> Response:
    """Captura errores de validacion de query/body/path.

    HTML para navegador, JSON estructurado (mismo formato que el default de
    FastAPI) para clientes API.
    """
    if _wants_html(request):
        return templates.TemplateResponse(
            request=request,
            name="error_422.html",
            context={},
            status_code=422,
        )
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(exc.errors())},
    )


def session_required_handler(
    request: Request, exc: SessionRequiredError
) -> RedirectResponse:
    """Traduce ``SessionRequiredError`` en redirect a ``/login`` con cookie limpia.

    La cookie se borra con flags fijos (``HttpOnly`` + ``SameSite=Lax``) y
    sin pasar por settings, porque los handlers de excepcion no pueden
    inyectar dependencies y ``get_settings()`` directo se saltaria los
    overrides en tests. ``Secure`` no afecta al borrado, solo al envio.
    """
    redirect = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    if SESSION_COOKIE_NAME in request.cookies:
        redirect.delete_cookie(
            key=SESSION_COOKIE_NAME,
            path="/",
            httponly=True,
            samesite="lax",
        )
    return redirect


# ---------------------------------------------------------------------------
# Helpers de cookie / respuesta
# ---------------------------------------------------------------------------


def _is_secure_env(settings: Settings) -> bool:
    """``True`` si la cookie debe llevar el flag ``Secure``.

    Solo en ``APP_ENV=production`` para que en desarrollo local (HTTP) la
    cookie no sea descartada por el navegador.
    """
    return settings.APP_ENV == "production"


def _set_session_cookie(response: Response, user_id: int, settings: Settings) -> None:
    token = create_session_token(user_id, secret_key=settings.APP_SECRET_KEY)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=_is_secure_env(settings),
        path="/",
    )


def _clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=_is_secure_env(settings),
    )


def _render_with_session(
    *,
    template: str,
    request: Request,
    user: User,
    settings: Settings,
    status_code: int = 200,
    **context: Any,
) -> Response:
    """Renderiza una plantilla y re-emite la cookie de sesion (sliding)."""
    response = templates.TemplateResponse(
        request=request,
        name=template,
        context={"user": user, **context},
        status_code=status_code,
    )
    assert user.id is not None  # garantizado por SQLite (PK AUTOINCREMENT)
    _set_session_cookie(response, user.id, settings)
    return response


def _redirect_anonymous(
    request: Request,
    settings: Settings,
    *,
    target: str = "/login",
) -> RedirectResponse:
    """Redirige a ``/login`` y limpia cualquier cookie stale.

    Solo se usa en rutas que **no** usan ``require_user_html`` (p. ej. ``/``,
    porque en ese caso queremos un redirect distinto segun haya sesion o no).
    """
    redirect = RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)
    if SESSION_COOKIE_NAME in request.cookies:
        _clear_session_cookie(redirect, settings)
    return redirect


def _client_ip(request: Request) -> str:
    """IP del cliente para el rate limit. ``request.client`` puede ser ``None``
    en tests; devolvemos ``"unknown"`` en ese caso."""
    return request.client.host if request.client else "unknown"


def _query_url(request: Request, **overrides: object) -> str:
    """Construye una URL relativa con los query params actuales + ``overrides``.

    Usado para los enlaces de paginacion y para el banner de filtros: cada
    enlace hereda los filtros activos y solo cambia los parametros indicados.
    Pasar ``key=None`` elimina ese parametro.
    """
    params: dict[str, str] = dict(request.query_params)
    for key, value in overrides.items():
        if value is None:
            params.pop(key, None)
        else:
            params[key] = str(value)
    qs = urlencode(params)
    return f"{request.url.path}?{qs}" if qs else request.url.path


# ---------------------------------------------------------------------------
# Filtros del listado
# ---------------------------------------------------------------------------

#: Statuses considerados "activos" cuando no se pasa filtro explicito. Se
#: muestran por defecto; los CLOSED quedan ocultos hasta que el usuario hace
#: clic en "Ver todos" (ver template).
_ACTIVE_STATUSES: tuple[TicketStatus, ...] = (
    TicketStatus.NEW,
    TicketStatus.IN_PROGRESS,
    TicketStatus.WAITING,
)


class StatusFilter(str, Enum):
    """Valores aceptados en ``?status=``. Incluye ``ALL`` (sintetico)."""

    NEW = "NEW"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING = "WAITING"
    CLOSED = "CLOSED"
    ALL = "ALL"


class CategoryFilter(str, Enum):
    """Valores aceptados en ``?category=``. ``UNCATEGORIZED`` filtra ``NULL``."""

    ADMINISTRATIVO = "ADMINISTRATIVO"
    COMERCIAL = "COMERCIAL"
    SOPORTE = "SOPORTE"
    UNCATEGORIZED = "UNCATEGORIZED"


_PAGE_SIZE = 50


# ---------------------------------------------------------------------------
# Rutas publicas / con awareness de sesion
# ---------------------------------------------------------------------------


@router.get("/", include_in_schema=False)
def root(
    request: Request,
    user: User | None = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Redirige al listado si hay sesion, a /login si no."""
    if user is None:
        return _redirect_anonymous(request, settings)
    redirect = RedirectResponse(url="/tickets", status_code=status.HTTP_303_SEE_OTHER)
    assert user.id is not None
    _set_session_cookie(redirect, user.id, settings)
    return redirect


@router.get("/login", include_in_schema=False)
def login_form(
    request: Request,
    user: User | None = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Muestra el formulario de login. Si ya hay sesion, redirige a /tickets."""
    if user is not None:
        redirect = RedirectResponse(url="/tickets", status_code=status.HTTP_303_SEE_OTHER)
        assert user.id is not None
        _set_session_cookie(redirect, user.id, settings)
        return redirect
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None, "username": ""},
    )


@router.post("/login", include_in_schema=False)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    settings: Settings = Depends(get_settings),
    conn: sqlite3.Connection = Depends(get_db_connection),
) -> Response:
    """Procesa el login.

    Orden:

    1. **Rate limit** por ``(ip, username)``. Si esta bloqueado, 429 sin
       tocar la BD ni bcrypt.
    2. **Verificacion de credenciales**. Si fallan, registramos el intento
       y volvemos a renderizar el formulario con error generico.
    3. **Exito**: reseteamos contadores, ponemos la cookie y redirigimos.
    """
    ip = _client_ip(request)

    retry_after = login_rate_limiter.check(ip, username)
    if retry_after > 0:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": (
                    f"Demasiados intentos. Espera {retry_after} segundos "
                    "antes de volver a probar."
                ),
                "username": username,
            },
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(retry_after)},
        )

    user = get_user_by_username(conn, username)
    valid = user is not None and verify_password(password, user.password_hash)

    if not valid or user is None:
        login_rate_limiter.record(ip, username)
        logger.info("login fallido: username=%r ip=%s", username, ip)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": "Usuario o contrasena incorrectos.",
                "username": username,
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    assert user.id is not None
    login_rate_limiter.reset(ip, username)
    redirect = RedirectResponse(url="/tickets", status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookie(redirect, user.id, settings)
    logger.info("login OK: username=%r ip=%s", username, ip)
    return redirect


@router.post("/logout", include_in_schema=False)
def logout(settings: Settings = Depends(get_settings)) -> RedirectResponse:
    """Cierra sesion borrando la cookie. Solo POST para evitar logouts por enlace
    cruzado."""
    redirect = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    _clear_session_cookie(redirect, settings)
    return redirect


# ---------------------------------------------------------------------------
# Rutas protegidas (require_user_html)
# ---------------------------------------------------------------------------


@router.get("/tickets", include_in_schema=False)
def tickets_list_view(
    request: Request,
    status_param: StatusFilter | None = Query(default=None, alias="status"),
    category_param: CategoryFilter | None = Query(default=None, alias="category"),
    needs_review: bool | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(require_user_html),
    settings: Settings = Depends(get_settings),
    conn: sqlite3.Connection = Depends(get_db_connection),
) -> Response:
    """Listado paginado con filtros. Sin ``status`` muestra solo los activos."""
    # Mapeo URL -> servicio.
    if status_param is None:
        statuses_arg: list[TicketStatus] | None = list(_ACTIVE_STATUSES)
        implicit_status_filter = True
    elif status_param is StatusFilter.ALL:
        statuses_arg = None
        implicit_status_filter = False
    else:
        statuses_arg = [TicketStatus(status_param.value)]
        implicit_status_filter = False

    if category_param is CategoryFilter.UNCATEGORIZED:
        only_uncategorized = True
        category_arg: TicketCategory | None = None
    elif category_param is not None:
        only_uncategorized = False
        category_arg = TicketCategory(category_param.value)
    else:
        only_uncategorized = False
        category_arg = None

    # Pedimos ``limit + 1`` para saber si hay siguiente sin un COUNT(*) extra.
    rows = list_tickets(
        conn,
        statuses=statuses_arg,
        category=category_arg,
        only_uncategorized=only_uncategorized,
        needs_review=needs_review,
        limit=_PAGE_SIZE + 1,
        offset=offset,
    )
    has_next = len(rows) > _PAGE_SIZE
    tickets = rows[:_PAGE_SIZE]

    next_offset = offset + _PAGE_SIZE if has_next else None
    prev_offset = max(offset - _PAGE_SIZE, 0) if offset > 0 else None

    return _render_with_session(
        template="tickets_list.html",
        request=request,
        user=user,
        settings=settings,
        tickets=tickets,
        # Filtros activos (lo que se ve seleccionado en el form).
        status_value=status_param.value if status_param else "",
        category_value=category_param.value if category_param else "",
        needs_review_value=(
            "" if needs_review is None else ("true" if needs_review else "false")
        ),
        implicit_status_filter=implicit_status_filter,
        # Helpers para construir URLs de paginacion / banner.
        url_show_all=_query_url(request, status="ALL", offset=None),
        url_clear_filters=request.url.path,
        url_next=(_query_url(request, offset=next_offset) if has_next else None),
        url_prev=(
            _query_url(request, offset=prev_offset if prev_offset else None)
            if prev_offset is not None
            else None
        ),
        page_size=_PAGE_SIZE,
        offset=offset,
    )


# El formato de los IDs (``TLY-2026-0001``) lo controla el path: el
# parametro de FastAPI es libre, pero lo validamos con la misma regex
# que usa ``ticket_service`` para no malgastar query si la URL viene rota.
from app.services.ticket_service import _ID_RE  # noqa: E402


def _can_reply(ticket: Any) -> bool:
    """Indica si el ticket es respondible (canal email + email + msg_id).

    El template lo usa para deshabilitar el form en tickets demo o de
    otros canales. El handler POST tambien lo valida defensivamente.
    """
    return (
        ticket.channel.value == "email"
        and bool(ticket.from_email)
        and bool(ticket.raw_message_id)
    )


def _render_ticket_detail(
    *,
    request: Request,
    user: User,
    settings: Settings,
    conn: sqlite3.Connection,
    ticket_id: str,
    reply_error: str | None = None,
    reply_body: str = "",
    status_code: int = 200,
) -> Response:
    """Renderiza el detalle con ticket + historial. 404 si no existe.

    Usado por el ``GET`` y por el ``POST`` cuando hay que re-renderizar
    tras un error de validacion (conserva el body que escribio el
    operador para que no lo pierda).
    """
    if not _ID_RE.match(ticket_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    ticket = get_ticket(conn, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    replies = get_replies(conn, ticket_id)
    return _render_with_session(
        template="ticket_detail.html",
        request=request,
        user=user,
        settings=settings,
        status_code=status_code,
        ticket=ticket,
        replies=replies,
        can_reply=_can_reply(ticket),
        reply_error=reply_error,
        reply_body=reply_body,
    )


@router.get("/tickets/{ticket_id}", include_in_schema=False)
def ticket_detail_view(
    ticket_id: str,
    request: Request,
    user: User = Depends(require_user_html),
    settings: Settings = Depends(get_settings),
    conn: sqlite3.Connection = Depends(get_db_connection),
) -> Response:
    """Vista detalle. 404 si el formato no encaja o si no existe en BD."""
    return _render_ticket_detail(
        request=request,
        user=user,
        settings=settings,
        conn=conn,
        ticket_id=ticket_id,
    )


def _parse_new_status(value: str) -> TicketStatus | None:
    """Convierte el campo del form a ``TicketStatus | None``.

    Cadena vacia o ``"NO_CHANGE"`` -> ``None``. Cualquier otro valor se
    intenta como ``TicketStatus``; si no encaja -> ``ValueError``.
    """
    if not value or value == "NO_CHANGE":
        return None
    try:
        return TicketStatus(value)
    except ValueError as exc:
        raise ValueError(f"status invalido: {value!r}") from exc


@router.post("/tickets/{ticket_id}/reply", include_in_schema=False)
def ticket_reply_submit(
    ticket_id: str,
    request: Request,
    body: str = Form(...),
    new_status: str = Form(""),
    user: User = Depends(require_user_html),
    settings: Settings = Depends(get_settings),
    conn: sqlite3.Connection = Depends(get_db_connection),
    email_client: EmailSender = Depends(get_email_sender),
) -> Response:
    """Envia una respuesta al cliente. PRG en exito, re-render en error.

    - Validacion fallida (body vacio, ticket no respondible, status raro)
      -> 200 + re-render con ``reply_error`` y el body conservado.
    - Envio SMTP fallido -> 200 + re-render con mensaje generico (el
      operador puede reintentar).
    - Exito -> 303 a ``GET /tickets/{id}`` con la cookie de sesion
      renovada (PRG).
    """
    if not _ID_RE.match(ticket_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    ticket = get_ticket(conn, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    # Validacion del campo status antes de tocar SMTP.
    try:
        parsed_status = _parse_new_status(new_status)
    except ValueError as exc:
        return _render_ticket_detail(
            request=request,
            user=user,
            settings=settings,
            conn=conn,
            ticket_id=ticket_id,
            reply_error=str(exc),
            reply_body=body,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        send_reply(
            conn=conn,
            email_client=email_client,
            ticket=ticket,
            body=body,
            user=user,
            new_status=parsed_status,
        )
    except ValueError as exc:
        # Body vacio, ticket no respondible, etc. Mensaje del servicio
        # ya es legible para el operador.
        return _render_ticket_detail(
            request=request,
            user=user,
            settings=settings,
            conn=conn,
            ticket_id=ticket_id,
            reply_error=str(exc),
            reply_body=body,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as exc:  # noqa: BLE001 — defensa: SMTP, red, lo que sea
        logger.exception(
            "ticket_reply_submit fallo ticket=%s reason=%s",
            ticket_id,
            type(exc).__name__,
        )
        return _render_ticket_detail(
            request=request,
            user=user,
            settings=settings,
            conn=conn,
            ticket_id=ticket_id,
            reply_error=(
                "No se pudo enviar el email. Revisa la conexion SMTP y "
                "reintenta. Si el problema persiste, mira los logs."
            ),
            reply_body=body,
            status_code=status.HTTP_502_BAD_GATEWAY,
        )

    # Exito: PRG. Redirigimos al GET para que el operador vea la respuesta
    # en el historial sin riesgo de re-submit por refresh.
    redirect = RedirectResponse(
        url=f"/tickets/{ticket_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    assert user.id is not None
    _set_session_cookie(redirect, user.id, settings)
    return redirect
