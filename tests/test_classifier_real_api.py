"""Smoke test contra la **API real** de Anthropic.

Excluido por defecto via ``pyproject.toml`` (``addopts = -m 'not real_api'``).
Para ejecutarlo manualmente:

    pytest -m real_api

Coste estimado: < 0.001 EUR por ejecucion (Haiku 4.5).

Solo un caso inequivoco. Aqui no validamos la precision del modelo en zonas
grises — eso lo veriamos en una eval batch dedicada — solo que el flujo
end-to-end funciona: SDK + tool use + parseo + ClassificationResult.
"""

from __future__ import annotations

import os

import pytest

from app.config import get_settings
from app.models.ticket import TicketCategory
from app.services.classifier import classify


pytestmark = pytest.mark.real_api


@pytest.fixture(autouse=True)
def _skip_si_no_hay_api_key() -> None:
    """Salta el test si ``ANTHROPIC_API_KEY`` no esta presente en el entorno."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        # Limpiar cache porque otros tests pueden haber cacheado settings con
        # una clave fake; aqui exigimos una real.
        get_settings.cache_clear()
        pytest.skip("ANTHROPIC_API_KEY no presente; saltando smoke real.")
    get_settings.cache_clear()


def test_clasifica_incidencia_de_soporte_contra_api_real() -> None:
    """Caso inequivoco: averia tecnica de telefono IP -> SOPORTE con alta confianza."""
    res = classify(
        subject="El telefono IP de recepcion no da tono",
        body=(
            "Buenos dias,\n\n"
            "Desde esta manyana el telefono IP de recepcion no da tono al descolgar. "
            "Las luces estan encendidas y el cable de red parece bien conectado. "
            "He probado a reiniciarlo dos veces sin resultado.\n\n"
            "Gracias."
        ),
        ticket_id="SMOKE-REAL-API",
    )
    assert res.succeeded, f"clasificacion fallida: {res}"
    assert res.category is TicketCategory.SOPORTE
    assert res.confidence is not None and res.confidence > 0.7
    assert res.reasoning is not None and len(res.reasoning) > 0
