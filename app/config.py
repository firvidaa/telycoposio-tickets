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

from pydantic import Field, ValidationError, field_validator, model_validator
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
    # URL base de la aplicacion. Se usa en las notificaciones internas para
    # construir enlaces clickables al detalle de cada ticket. Default valido
    # para desarrollo local; en produccion se override en ``.env``.
    APP_BASE_URL: str = "http://localhost:8000"

    # Flag que activa el worker de polling de email (Paso 8). Por defecto
    # esta **desactivado** para que los tests y los arranques manuales en
    # dev no toquen la cuenta de Gmail real sin querer. Hay que ponerlo a
    # ``true`` explicitamente en ``.env`` para encenderlo.
    WORKER_ENABLED: bool = False

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
    # Google Sheets (Paso 9)
    # =====================================================================
    # Flag de activacion paralelo a WORKER_ENABLED: por defecto **off** para
    # que tests y `uvicorn --reload` no toquen Sheets sin querer. Hay que
    # ponerlo a ``true`` explicitamente en ``.env`` para encender el job.
    GSHEETS_ENABLED: bool = False
    GSHEETS_SPREADSHEET_ID: str | None = None
    GSHEETS_CREDENTIALS_PATH: str | None = None
    GSHEETS_WORKSHEET_NAME: str = "Tickets"
    # 5 minutos. Para 200 tickets/mes (~7/dia) es de sobra; ciclos mas
    # frecuentes solo gastan cuota API sin ganar nada.
    GSHEETS_SYNC_INTERVAL_SECONDS: int = 300

    # ---------------------------------------------------------------------
    # Validators
    # ---------------------------------------------------------------------

    @field_validator("APP_BASE_URL")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        """Quita ``/`` final para evitar URLs con doble slash.

        Si alguien pone ``http://localhost:8000/`` en ``.env``, sin esto
        terminariamos generando enlaces tipo ``http://localhost:8000//tickets/...``.
        """
        return v.rstrip("/")

    @model_validator(mode="after")
    def _check_gsheets_consistency(self) -> Settings:
        """Si ``GSHEETS_ENABLED=true``, exigir ID y credenciales.

        Fallar al cargar la config evita un arranque "exitoso" cuya
        primera iteracion del job revienta con un mensaje feo de gspread.
        Mensaje accionable: indica exactamente que falta.
        """
        if not self.GSHEETS_ENABLED:
            return self
        faltan: list[str] = []
        if not self.GSHEETS_SPREADSHEET_ID:
            faltan.append("GSHEETS_SPREADSHEET_ID")
        if not self.GSHEETS_CREDENTIALS_PATH:
            faltan.append("GSHEETS_CREDENTIALS_PATH")
        if faltan:
            raise ValueError(
                "GSHEETS_ENABLED=true requiere "
                + " y ".join(faltan)
                + ". Anyadelas a .env o pon GSHEETS_ENABLED=false."
            )
        return self


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
