"""Rutas web: login, logout y placeholder de ``/tickets``.

El listado y el detalle reales de tickets llegan en el Paso 5b. En este
paso ya hay una ruta ``/tickets`` minima que requiere login y muestra un
mensaje de bienvenida; sirve para verificar end-to-end el flujo de
autenticacion antes de tocar la UI de tickets.

Conexion a SQLite via dependency ``get_db_connection``: se abre una
conexion por request y se cierra al salir. El acceso a usuarios pasa por
``app.services.auth`` (modulo puro, no acoplado a FastAPI).

Sliding renewal de la cookie de sesion: cada handler protegido llama a
``_render_with_session`` (o re-emite la cookie en su redirect) para que un
empleado activo nunca pierda la sesion. La logica vive en los handlers y
no en la dependency porque cuando se devuelve un ``TemplateResponse``
directamente, los headers que la dependency ponga en su ``Response``
inyectada no se propagan a la respuesta final.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import Settings, get_settings
from app.db.sqlite import get_connection
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


logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router = APIRouter()


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_db_connection(
    settings: Settings = Depends(get_settings),
) -> Iterator[sqlite3.Connection]:
    """Abre una conexion SQLite por request y la cierra al terminar."""
    with get_connection(settings.SQLITE_PATH) as conn:
        yield conn


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


def require_user(user: User | None = Depends(current_user)) -> User:
    """Dependency estricta: 401 si no hay sesion. Pensada para APIs JSON o tests.

    Las rutas HTML usan ``current_user`` y deciden ellas mismas si renderizar
    o redirigir, porque desde una ``HTTPException`` no se puede devolver un
    ``RedirectResponse``.
    """
    if user is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user


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
    **context: Any,
) -> Response:
    """Renderiza una plantilla y re-emite la cookie de sesion (sliding)."""
    response = templates.TemplateResponse(
        request=request,
        name=template,
        context={"user": user, **context},
    )
    assert user.id is not None  # garantizado por SQLite (PK AUTOINCREMENT)
    _set_session_cookie(response, user.id, settings)
    return response


def _redirect_unauthenticated(
    request: Request,
    settings: Settings,
    *,
    target: str = "/login",
) -> RedirectResponse:
    """Redirige al ``target`` y limpia cualquier cookie de sesion stale.

    Si llegamos aqui con cookie pero ``current_user`` devolvio ``None``, la
    cookie es invalida (firma mala, expirada, o uid huerfano). La quitamos
    para no molestar al cliente en cada request.
    """
    redirect = RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)
    if SESSION_COOKIE_NAME in request.cookies:
        _clear_session_cookie(redirect, settings)
    return redirect


def _client_ip(request: Request) -> str:
    """IP del cliente para el rate limit. ``request.client`` puede ser ``None``
    en tests; devolvemos ``"unknown"`` en ese caso."""
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------


@router.get("/", include_in_schema=False)
def root(
    request: Request,
    user: User | None = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Redirige al listado si hay sesion, a /login si no."""
    if user is None:
        return _redirect_unauthenticated(request, settings)
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

    Orden de comprobaciones:

    1. **Rate limit** por ``(ip, username)``. Si esta bloqueado, respondemos
       429 sin tocar la BD ni bcrypt (bcrypt es caro a proposito).
    2. **Verificacion de credenciales**. Si fallan, registramos el intento
       en el rate limiter y volvemos a renderizar el formulario con error
       generico (no distinguimos "usuario no existe" vs. "password mala").
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
    """Cierra sesion borrando la cookie. Acepta solo POST para evitar logouts
    por enlace cruzado."""
    redirect = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    _clear_session_cookie(redirect, settings)
    return redirect


@router.get("/tickets", include_in_schema=False)
def tickets_placeholder(
    request: Request,
    user: User | None = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Placeholder hasta el Paso 5b. Si no hay sesion, redirige a /login."""
    if user is None:
        return _redirect_unauthenticated(request, settings)
    return _render_with_session(
        template="tickets_placeholder.html",
        request=request,
        user=user,
        settings=settings,
    )
