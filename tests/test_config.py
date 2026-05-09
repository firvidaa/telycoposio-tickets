"""Tests de la carga de configuracion (``app.config``).

Usamos ``Settings(_env_file=None)`` o ``monkeypatch.chdir(tmp_path)`` para no
depender del ``.env`` real del proyecto (que puede tener una APP_SECRET_KEY
valida y enmascarar tests negativos). Las variables se inyectan via
``monkeypatch.setenv``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings


# ---------------------------------------------------------------------------
# Carga correcta
# ---------------------------------------------------------------------------


def test_carga_con_variables_criticas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    s = Settings(_env_file=None)
    assert s.APP_SECRET_KEY == "x" * 32
    assert s.ANTHROPIC_API_KEY == "sk-ant-test"


def test_defaults_de_variables_opcionales(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    s = Settings(_env_file=None)
    assert s.APP_ENV == "production"
    assert s.APP_HOST == "0.0.0.0"
    assert s.APP_PORT == 8000
    assert s.TICKET_PREFIX == "TLY"
    assert s.SQLITE_PATH == "data/app.db"
    assert s.GMAIL_POLL_INTERVAL_SECONDS == 60
    assert s.ANTHROPIC_MODEL == "claude-haiku-4-5-20251001"


def test_opcionales_sin_default_son_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    s = Settings(_env_file=None)
    assert s.GMAIL_ADDRESS is None
    assert s.GMAIL_CREDENTIALS_PATH is None
    assert s.GMAIL_TOKEN_PATH is None
    assert s.INTERNAL_NOTIFICATION_EMAIL is None
    assert s.GSHEETS_SPREADSHEET_ID is None
    assert s.GSHEETS_CREDENTIALS_PATH is None


def test_overrides_desde_entorno(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("APP_PORT", "9999")
    monkeypatch.setenv("TICKET_PREFIX", "TLC")
    monkeypatch.setenv("GMAIL_ADDRESS", "foo@bar.com")
    s = Settings(_env_file=None)
    assert s.APP_PORT == 9999
    assert s.TICKET_PREFIX == "TLC"
    assert s.GMAIL_ADDRESS == "foo@bar.com"


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
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        get_settings()
    err = capsys.readouterr().err
    # El mensaje debe contener el comando exacto para generar la clave.
    assert "secrets.token_urlsafe(32)" in err
    assert "APP_SECRET_KEY" in err


def test_falla_si_app_secret_key_es_corta(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_SECRET_KEY", "demasiado-corta")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        get_settings()
    err = capsys.readouterr().err
    assert "secrets.token_urlsafe(32)" in err


def test_falla_si_falta_anthropic_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_SECRET_KEY", "x" * 32)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    # Pydantic enumera el campo en el error.
    assert any(
        err["loc"] and err["loc"][0] == "ANTHROPIC_API_KEY"
        for err in excinfo.value.errors()
    )


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def test_get_settings_cachea(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    get_settings.cache_clear()
    s1 = get_settings()
    s2 = get_settings()
    # Misma instancia: lru_cache devuelve el objeto cacheado.
    assert s1 is s2
