"""Tests del clasificador (``app.services.classifier``).

Sin red. El cliente Anthropic se sustituye por uno falso que devuelve un
``response`` predefinido o lanza la excepcion deseada. La firma del cliente
falso replica solo lo que el codigo bajo test usa: ``messages.create(**kwargs)``.
"""

from __future__ import annotations

import logging
from typing import Any

import anthropic
import httpx
import pytest

from app.config import Settings
from app.models.ticket import TicketCategory
from app.services.classifier import (
    BODY_TRUNCATE_LIMIT,
    CLASSIFICATION_TOOL,
    SYSTEM_PROMPT,
    TOOL_NAME,
    ClassificationResult,
    classify,
)


# ---------------------------------------------------------------------------
# Doubles del SDK
# ---------------------------------------------------------------------------


class _ToolUseBlock:
    def __init__(self, name: str, input_data: dict[str, Any]) -> None:
        self.type = "tool_use"
        self.name = name
        self.input = input_data


class _TextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Usage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _Response:
    def __init__(self, content: list[Any], usage: _Usage) -> None:
        self.content = content
        self.usage = usage


def _ok_response(
    *,
    category: str = "SOPORTE",
    confidence: float = 0.9,
    reasoning: str = "razon de prueba",
    input_tokens: int = 100,
    output_tokens: int = 20,
) -> _Response:
    return _Response(
        content=[
            _ToolUseBlock(
                TOOL_NAME,
                {
                    "category": category,
                    "confidence": confidence,
                    "reasoning": reasoning,
                },
            )
        ],
        usage=_Usage(input_tokens, output_tokens),
    )


class _FakeMessages:
    def __init__(self, response_or_exc: Any) -> None:
        self._response_or_exc = response_or_exc
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self._response_or_exc, BaseException):
            raise self._response_or_exc
        return self._response_or_exc


class _FakeClient:
    def __init__(self, response_or_exc: Any) -> None:
        self.messages = _FakeMessages(response_or_exc)


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        APP_SECRET_KEY="x" * 32,
        ANTHROPIC_API_KEY="sk-ant-test",
        ANTHROPIC_MODEL="claude-haiku-4-5-20251001",
    )


# ---------------------------------------------------------------------------
# Caso feliz
# ---------------------------------------------------------------------------


def test_clasifica_correctamente_un_ticket_de_soporte() -> None:
    client = _FakeClient(_ok_response(category="SOPORTE", confidence=0.92))
    res = classify(
        subject="No me funciona el telefono",
        body="No da tono al descolgar",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert res.succeeded
    assert res.category is TicketCategory.SOPORTE
    assert res.confidence == pytest.approx(0.92)
    assert res.reasoning == "razon de prueba"


def test_pasa_modelo_system_y_tool_choice_correctamente() -> None:
    client = _FakeClient(_ok_response())
    classify(
        subject="X",
        body="Y",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    call = client.messages.calls[0]
    assert call["model"] == "claude-haiku-4-5-20251001"
    assert call["system"] == SYSTEM_PROMPT
    assert call["tools"] == [CLASSIFICATION_TOOL]
    assert call["tool_choice"] == {"type": "tool", "name": TOOL_NAME}
    assert call["timeout"] == 30.0


def test_user_message_envuelve_subject_y_body_en_etiquetas() -> None:
    client = _FakeClient(_ok_response())
    classify(
        subject="ASUNTO",
        body="CUERPO",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    user_msg = client.messages.calls[0]["messages"][0]["content"]
    assert "<ticket>" in user_msg
    assert "<subject>ASUNTO</subject>" in user_msg
    assert "<body>CUERPO</body>" in user_msg


# ---------------------------------------------------------------------------
# Truncado
# ---------------------------------------------------------------------------


def test_body_largo_se_trunca() -> None:
    long_body = "x" * (BODY_TRUNCATE_LIMIT + 500)
    client = _FakeClient(_ok_response())
    classify(
        subject="s",
        body=long_body,
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    user_msg = client.messages.calls[0]["messages"][0]["content"]
    # Truncado limpio: la cantidad exacta de 'x' es BODY_TRUNCATE_LIMIT.
    assert user_msg.count("x") == BODY_TRUNCATE_LIMIT
    # Sin marcador "..." que ocuparia tokens innecesarios.
    assert "..." not in user_msg


def test_body_corto_no_se_trunca() -> None:
    body = "mensaje corto"
    client = _FakeClient(_ok_response())
    classify(
        subject="s",
        body=body,
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    user_msg = client.messages.calls[0]["messages"][0]["content"]
    assert "<body>mensaje corto</body>" in user_msg


# ---------------------------------------------------------------------------
# Errores de la API
# ---------------------------------------------------------------------------


def _httpx_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def test_api_timeout_error_devuelve_failed() -> None:
    err = anthropic.APITimeoutError(request=_httpx_request())
    client = _FakeClient(err)
    res = classify(
        subject="s",
        body="b",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert res == ClassificationResult.failed()


def test_api_connection_error_devuelve_failed() -> None:
    err = anthropic.APIConnectionError(request=_httpx_request())
    client = _FakeClient(err)
    res = classify(
        subject="s",
        body="b",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert not res.succeeded


def test_api_error_generica_devuelve_failed() -> None:
    err = anthropic.APIError(
        message="boom", request=_httpx_request(), body=None
    )
    client = _FakeClient(err)
    res = classify(
        subject="s",
        body="b",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert not res.succeeded


def test_excepcion_inesperada_devuelve_failed() -> None:
    """Cualquier cosa fuera del SDK tambien cae al fallback de seguridad."""
    client = _FakeClient(RuntimeError("error random"))
    res = classify(
        subject="s",
        body="b",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert not res.succeeded


# ---------------------------------------------------------------------------
# Errores de parseo del response
# ---------------------------------------------------------------------------


def test_response_sin_tool_use_devuelve_failed() -> None:
    """Si el modelo devuelve solo texto en lugar de llamar a la tool, fallo."""
    response = _Response(
        content=[_TextBlock("ADMINISTRATIVO probablemente")],
        usage=_Usage(50, 5),
    )
    client = _FakeClient(response)
    res = classify(
        subject="s",
        body="b",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert not res.succeeded


def test_response_con_categoria_invalida_devuelve_failed() -> None:
    response = _Response(
        content=[
            _ToolUseBlock(
                TOOL_NAME,
                {"category": "MARKETING", "confidence": 0.9, "reasoning": "x"},
            )
        ],
        usage=_Usage(50, 5),
    )
    client = _FakeClient(response)
    res = classify(
        subject="s",
        body="b",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert not res.succeeded


def test_response_con_confidence_fuera_de_rango_devuelve_failed() -> None:
    """Defensa en profundidad aunque el schema de la tool ya restringe [0,1]."""
    response = _Response(
        content=[
            _ToolUseBlock(
                TOOL_NAME,
                {"category": "SOPORTE", "confidence": 1.5, "reasoning": "x"},
            )
        ],
        usage=_Usage(50, 5),
    )
    client = _FakeClient(response)
    res = classify(
        subject="s",
        body="b",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert not res.succeeded


def test_response_sin_keys_requeridas_devuelve_failed() -> None:
    response = _Response(
        content=[_ToolUseBlock(TOOL_NAME, {"category": "SOPORTE"})],
        usage=_Usage(50, 5),
    )
    client = _FakeClient(response)
    res = classify(
        subject="s",
        body="b",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert not res.succeeded


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def test_loguea_metricas_en_exito(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.services.classifier")
    client = _FakeClient(
        _ok_response(
            category="SOPORTE",
            confidence=0.91,
            input_tokens=350,
            output_tokens=85,
        )
    )
    classify(
        subject="s",
        body="b",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
        ticket_id="TLY-2026-0042",
    )
    text = caplog.text
    assert "ticket=TLY-2026-0042" in text
    assert "category=SOPORTE" in text
    assert "confidence=0.91" in text
    assert "tokens_in=350" in text
    assert "tokens_out=85" in text
    assert "latency_ms=" in text


def test_loguea_warning_en_fallo(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="app.services.classifier")
    client = _FakeClient(anthropic.APITimeoutError(request=_httpx_request()))
    classify(
        subject="s",
        body="b",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
        ticket_id="TLY-2026-0042",
    )
    text = caplog.text
    assert "WARNING" in text
    assert "ticket=TLY-2026-0042" in text
    assert "APITimeoutError" in text


def test_no_loguea_subject_ni_body(caplog: pytest.LogCaptureFixture) -> None:
    """SPEC §9: los logs no deben contener cuerpo ni subject completos."""
    caplog.set_level(logging.DEBUG, logger="app.services.classifier")
    client = _FakeClient(_ok_response())
    secret_subject = "ASUNTO_QUE_NO_DEBE_APARECER_EN_LOGS"
    secret_body = "CUERPO_QUE_NO_DEBE_APARECER_EN_LOGS"
    classify(
        subject=secret_subject,
        body=secret_body,
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert secret_subject not in caplog.text
    assert secret_body not in caplog.text


# ---------------------------------------------------------------------------
# ClassificationResult
# ---------------------------------------------------------------------------


def test_failed_result_tiene_succeeded_false() -> None:
    r = ClassificationResult.failed()
    assert r.category is None
    assert r.confidence is None
    assert r.reasoning is None
    assert r.succeeded is False


def test_resultado_con_categoria_es_succeeded() -> None:
    r = ClassificationResult(
        category=TicketCategory.SOPORTE, confidence=0.9, reasoning="x"
    )
    assert r.succeeded is True
