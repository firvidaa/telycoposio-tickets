"""Servicio de autenticacion: passwords, sesiones por cookie y rate limiting.

Tres responsabilidades, todas puras (no tocan FastAPI directamente; ese acoplamiento
vive en ``app/web/routes.py``):

1. **Hash y verificacion de password con bcrypt.** Coste por defecto del paquete
   ``bcrypt`` (``gensalt()`` sin argumentos). Igual que ``scripts/add_user.py``.

2. **Sesiones via cookie firmada con itsdangerous.** No hay tabla ``sessions``
   (ver SPEC §4.3 / §9 v1.2). El payload de la cookie es ``{"uid": <user_id>}``,
   serializado con ``URLSafeTimedSerializer`` usando ``APP_SECRET_KEY``. La
   verificacion impone ``max_age = SESSION_MAX_AGE_SECONDS`` (30 dias). En cada
   request autenticada se re-emite la cookie con timestamp nuevo (sliding):
   asi un empleado que entra a diario nunca pierde sesion, pero una sesion
   abandonada caduca por inactividad. Para invalidar **todas** las sesiones a
   la vez (despido, sospecha de fuga) basta rotar ``APP_SECRET_KEY``.

3. **Rate limit de login en memoria por (IP, username).** SPEC §9 fija 5
   intentos/minuto. Por (IP, username) en lugar de solo IP porque los 3
   empleados saldran detras de la misma IP de oficina y un fallo de uno no
   debe bloquear a los otros. La estructura es un dict module-level con
   lock; suficiente para 3 usuarios y un proceso. Si pasamos a multi-worker
   habra que mover el limiter a un store compartido (Redis, SQLite),
   apuntado en SPEC §9 / fase futura.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Final

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.models.user import User, from_db_row


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

#: Nombre fijo de la cookie de sesion. Cambiarlo invalida sesiones antiguas
#: del lado cliente, igual que rotar la secret key.
SESSION_COOKIE_NAME: Final[str] = "tly_session"

#: Salt para la firma. itsdangerous permite varios usos del mismo secret con
#: salts distintos; usamos uno explicito por si en el futuro firmamos otra
#: cosa con la misma APP_SECRET_KEY.
_SESSION_SALT: Final[str] = "tly-session-v1"

#: Duracion de la cookie con renovacion deslizante. 30 dias es el limite
#: superior: si el empleado no entra durante 30 dias seguidos, expira.
SESSION_MAX_AGE_SECONDS: Final[int] = 60 * 60 * 24 * 30

#: Limites de login por (IP, username). SPEC §9.
LOGIN_MAX_ATTEMPTS: Final[int] = 5
LOGIN_WINDOW_SECONDS: Final[int] = 60


# ---------------------------------------------------------------------------
# Passwords (bcrypt)
# ---------------------------------------------------------------------------


def hash_password(plain: str) -> str:
    """Devuelve el hash bcrypt de ``plain`` (string, no bytes)."""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, password_hash: str) -> bool:
    """``True`` si ``plain`` corresponde a ``password_hash``.

    Si el hash esta corrupto o no es bcrypt valido devolvemos ``False`` en
    lugar de propagar la excepcion: tratamos un hash invalido como un fallo
    de credenciales mas, sin filtrar al cliente que la BD esta mal.
    """
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Cookie de sesion (itsdangerous)
# ---------------------------------------------------------------------------


def _serializer(secret_key: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key, salt=_SESSION_SALT)


def create_session_token(user_id: int, *, secret_key: str) -> str:
    """Firma ``{"uid": user_id}`` con el secret y devuelve el token serializado."""
    return _serializer(secret_key).dumps({"uid": user_id})


def verify_session_token(
    token: str,
    *,
    secret_key: str,
    max_age: int = SESSION_MAX_AGE_SECONDS,
) -> int | None:
    """Devuelve el ``user_id`` si la firma y el TTL son validos, o ``None``.

    No distinguimos entre token expirado y firma invalida: cualquiera de los
    dos casos significa "no logueado" y la respuesta a /login es la misma.
    """
    try:
        payload = _serializer(secret_key).loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, dict):
        return None
    uid = payload.get("uid")
    if not isinstance(uid, int):
        return None
    return uid


# ---------------------------------------------------------------------------
# Acceso a usuarios (lectura)
# ---------------------------------------------------------------------------


def get_user_by_username(conn: sqlite3.Connection, username: str) -> User | None:
    """Carga un usuario por ``username``. Devuelve ``None`` si no existe."""
    row = conn.execute(
        "SELECT * FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    return None if row is None else from_db_row(row)


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> User | None:
    """Carga un usuario por ``id``. Devuelve ``None`` si no existe."""
    row = conn.execute(
        "SELECT * FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    return None if row is None else from_db_row(row)


# ---------------------------------------------------------------------------
# Rate limit de login en memoria
# ---------------------------------------------------------------------------


@dataclass
class LoginRateLimiter:
    """Limita intentos de login por ``(ip, username)`` en una ventana deslizante.

    Estado en memoria: un dict ``(ip, username) -> [timestamps]``. Cada
    operacion descarta timestamps mas viejos que la ventana antes de decidir.
    """

    max_attempts: int = LOGIN_MAX_ATTEMPTS
    window_seconds: int = LOGIN_WINDOW_SECONDS
    _attempts: dict[tuple[str, str], list[float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _prune(self, key: tuple[str, str], now: float) -> list[float]:
        cutoff = now - self.window_seconds
        bucket = [t for t in self._attempts.get(key, []) if t > cutoff]
        if bucket:
            self._attempts[key] = bucket
        else:
            self._attempts.pop(key, None)
        return bucket

    def check(self, ip: str, username: str, *, now: float | None = None) -> int:
        """Devuelve segundos para esperar (``0`` si esta permitido).

        No registra el intento; solo consulta. Para registrar usa ``record``.
        """
        if now is None:
            now = time.monotonic()
        key = (ip, username)
        with self._lock:
            bucket = self._prune(key, now)
            if len(bucket) < self.max_attempts:
                return 0
            # Cuando expire el intento mas antiguo se libera un hueco.
            oldest = bucket[0]
            retry_after = int(self.window_seconds - (now - oldest)) + 1
            return max(retry_after, 1)

    def record(self, ip: str, username: str, *, now: float | None = None) -> None:
        """Anade un timestamp al bucket de ``(ip, username)``."""
        if now is None:
            now = time.monotonic()
        key = (ip, username)
        with self._lock:
            self._prune(key, now)
            self._attempts.setdefault(key, []).append(now)

    def reset(self, ip: str, username: str) -> None:
        """Limpia el contador de ``(ip, username)`` (tras un login correcto)."""
        with self._lock:
            self._attempts.pop((ip, username), None)


#: Instancia global usada por las rutas. Los tests deben crear la suya propia
#: para no compartir estado entre tests.
login_rate_limiter = LoginRateLimiter()
