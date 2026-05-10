"""Configuracion centralizada cargada desde ``.env`` y entorno del sistema.

Niveles:

- **Criticos** (la app no arranca sin ellos): ``APP_SECRET_KEY``,
  ``ANTHROPIC_API_KEY``.
- **Opcionales con default**: ``APP_HOST``, ``APP_PORT``,
  ``GMAIL_POLL_INTERVAL_SECONDS``, ``TICKET_PREFIX``, ``SQLITE_PATH``,
  ``ANTHROPIC_MODEL``, ``APP_ENV``.
- **Opcionales sin default**: ``GMAIL_*``, ``GSHEETS_*``,
  ``INTERNAL_NOTIFICATION_EMAIL``. Se rellenan cuando integramos cada
  servicio (Gmail en Paso N, Sheets en Paso M, ...).

Si ``APP_SECRET_KEY`` no esta definida o tiene menos de 32 caracteres, la
inicializacion falla con un mensaje accionable que indica el comando exacto
para generar una clave nueva.
"""

from __future__ import annotations

import sys
from functools import lru_cache

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Variables de entorno tipadas. Ver SPEC.md §7."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # =====================================================================
    # Criticos (sin default; la app no arranca si faltan)
    # =====================================================================
    APP_SECRET_KEY: str = Field(min_length=32)
    ANTHROPIC_API_KEY: str = Field(min_length=1)
    EMAIL_ADDRESS: str = Field(min_length=1)
    EMAIL_APP_PASSWORD: str = Field(min_length=1)

    # =====================================================================
    # Aplicacion (con default razonable)
    # =====================================================================
    APP_ENV: str = "production"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    TICKET_PREFIX: str = "TLY"
    SQLITE_PATH: str = "data/app.db"

    # =====================================================================
    # Anthropic
    # =====================================================================
    ANTHROPIC_MODEL: str = "claude-haiku-4-5-20251001"

    # =====================================================================
    # Email — IMAP + SMTP con App Password (v1.3)
    # =====================================================================
    # Hosts/puertos por defecto a Gmail. Si algun dia migramos de proveedor,
    # se sobreescriben en ``.env``.
    EMAIL_IMAP_HOST: str = "imap.gmail.com"
    EMAIL_IMAP_PORT: int = 993
    EMAIL_SMTP_HOST: str = "smtp.gmail.com"
    EMAIL_SMTP_PORT: int = 587
    EMAIL_POLL_INTERVAL_SECONDS: int = 60
    INTERNAL_NOTIFICATION_EMAIL: str | None = None

    # =====================================================================
    # Google Sheets (opcional hasta que se integre la sincronizacion)
    # =====================================================================
    GSHEETS_SPREADSHEET_ID: str | None = None
    GSHEETS_CREDENTIALS_PATH: str | None = None


_SECRET_KEY_HINT = (
    "\nERROR: APP_SECRET_KEY no esta definida o es demasiado corta (minimo 32 caracteres).\n"
    "Genera una clave nueva con:\n\n"
    '    python -c "import secrets; print(secrets.token_urlsafe(32))"\n\n'
    "Y anyadela a tu .env como:\n\n"
    "    APP_SECRET_KEY=<valor_generado>\n\n"
)


def _is_secret_key_error(err: dict) -> bool:
    loc = err.get("loc") or ()
    return bool(loc) and loc[0] == "APP_SECRET_KEY"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Carga (y cachea) la configuracion. Lanza ``ValidationError`` si falta algo critico.

    Cuando el problema es ``APP_SECRET_KEY`` ausente/invalida, escribe primero
    en stderr un mensaje accionable con el comando exacto para generarla.
    """
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        if any(_is_secret_key_error(err) for err in exc.errors()):
            sys.stderr.write(_SECRET_KEY_HINT)
        raise
