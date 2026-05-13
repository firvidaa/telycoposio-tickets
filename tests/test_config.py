"""Tests de la carga de configuracion (``app.config``).

Cada test parte de un entorno con las 4 variables criticas presentes
(fixture ``_required_env``) y limpia la cache de ``get_settings``. Los
tests negativos borran explicitamente la variable que quieren probar.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setea las 4 variables obligatorias y limpia la cache.

    Cualquier test que quiera verificar el fallo por ausencia de una de
    ellas debe llamar a ``monkeypatch.delenv("...", raising=False)`` despues.
    """
    monkeypatch.setenv("APP_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("EMAIL_ADDRESS", "test@example.com")
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "abcdefghijklmnop")
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Carga correcta
# ---------------------------------------------------------------------------


def test_carga_con_variables_criticas() -> None:
    s = Settings(_env_file=None)
    assert s.APP_SECRET_KEY == "x" * 32
    assert s.ANTHROPIC_API_KEY == "sk-ant-test"
    assert s.EMAIL_ADDRESS == "test@example.com"
    assert s.EMAIL_APP_PASSWORD == "abcdefghijklmnop"


def test_defaults_de_variables_opcionales() -> None:
    s = Settings(_env_file=None)
    assert s.APP_ENV == "production"
    assert s.APP_HOST == "0.0.0.0"
    assert s.APP_PORT == 8000
    assert s.TICKET_PREFIX == "TLY"
    assert s.SQLITE_PATH == "data/app.db"
    assert s.ANTHROPIC_MODEL == "claude-haiku-4-5-20251001"
    # Defaults para email — Gmail (v1.3).
    assert s.EMAIL_IMAP_HOST == "imap.gmail.com"
    assert s.EMAIL_IMAP_PORT == 993
    assert s.EMAIL_SMTP_HOST == "smtp.gmail.com"
    assert s.EMAIL_SMTP_PORT == 587
    assert s.EMAIL_POLL_INTERVAL_SECONDS == 60


def test_opcionales_sin_default_son_none() -> None:
    s = Settings(_env_file=None)
    assert s.INTERNAL_NOTIFICATION_EMAIL is None
    assert s.GSHEETS_SPREADSHEET_ID is None
    assert s.GSHEETS_CREDENTIALS_PATH is None


def test_worker_enabled_default_false() -> None:
    """Por defecto el worker esta apagado: no debe arrancarse sin querer."""
    s = Settings(_env_file=None)
    assert s.WORKER_ENABLED is False


def test_app_base_url_default() -> None:
    s = Settings(_env_file=None)
    assert s.APP_BASE_URL == "http://localhost:8000"


def test_app_base_url_strip_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Si .env tiene 'http://x/' lo normalizamos a 'http://x' para que las
    URLs de notificacion interna no salgan con doble slash."""
    monkeypatch.setenv("APP_BASE_URL", "https://tickets.empresa.es/")
    s = Settings(_env_file=None)
    assert s.APP_BASE_URL == "https://tickets.empresa.es"


def test_app_base_url_strip_multiple_trailing_slashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_BASE_URL", "https://x.example.com///")
    s = Settings(_env_file=None)
    assert s.APP_BASE_URL == "https://x.example.com"


def test_overrides_desde_entorno(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_PORT", "9999")
    monkeypatch.setenv("TICKET_PREFIX", "TLC")
    monkeypatch.setenv("EMAIL_IMAP_HOST", "imap.otro.com")
    monkeypatch.setenv("EMAIL_SMTP_PORT", "465")
    s = Settings(_env_file=None)
    assert s.APP_PORT == 9999
    assert s.TICKET_PREFIX == "TLC"
    assert s.EMAIL_IMAP_HOST == "imap.otro.com"
    assert s.EMAIL_SMTP_PORT == 465


# ---------------------------------------------------------------------------
# Errores accionables
# ---------------------------------------------------------------------------


def test_falla_si_falta_app_secret_key(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    # chdir a un directorio sin .env para que pydantic-settings no caiga en
    # el .env real del proyecto y enmascare la ausencia de la variable.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("APP_SECRET_KEY", raising=False)
    with pytest.raises(ValidationError):
        get_settings()
    err = capsys.readouterr().err
    assert "secrets.token_urlsafe(32)" in err
    assert "APP_SECRET_KEY" in err


def test_falla_si_app_secret_key_es_corta(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_SECRET_KEY", "demasiado-corta")
    with pytest.raises(ValidationError):
        get_settings()
    err = capsys.readouterr().err
    assert "secrets.token_urlsafe(32)" in err


def test_falla_si_falta_anthropic_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    assert any(
        err["loc"] and err["loc"][0] == "ANTHROPIC_API_KEY"
        for err in excinfo.value.errors()
    )


def test_falla_si_falta_email_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EMAIL_ADDRESS", raising=False)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    assert any(
        err["loc"] and err["loc"][0] == "EMAIL_ADDRESS"
        for err in excinfo.value.errors()
    )


def test_falla_si_falta_email_app_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EMAIL_APP_PASSWORD", raising=False)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    assert any(
        err["loc"] and err["loc"][0] == "EMAIL_APP_PASSWORD"
        for err in excinfo.value.errors()
    )


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def test_get_settings_cachea() -> None:
    s1 = get_settings()
    s2 = get_settings()
    # Misma instancia: lru_cache devuelve el objeto cacheado.
    assert s1 is s2
