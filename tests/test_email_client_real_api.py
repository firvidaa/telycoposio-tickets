"""Smoke test contra **IMAP/SMTP reales** de Gmail con la App Password configurada.

Excluido por defecto via ``pyproject.toml`` (``addopts = -m 'not real_api'``).
Para ejecutarlo manualmente:

    pytest -m real_api

Operaciones del smoke (no destructivas, pero **mutan** el INBOX):

1. SMTP send: enviamos un mensaje desde nuestra cuenta a nosotros mismos
   con un asunto identificable (``[SMOKE TEST] ...``).
2. IMAP list: contamos no procesados (no aserto: solo verificamos que la
   conexion + la query van).
3. (Limpieza no automatica) — el mensaje enviado quedara en el INBOX. Si
   ejecutas el smoke a menudo, marca esos mensajes como procesados o
   borralos manualmente desde Gmail web.

Coste: cero. Riesgo: tu INBOX recibe un mensaje por ejecucion.
"""

from __future__ import annotations

import os
import time

import pytest

from app.config import get_settings
from app.services.email_client import EmailClient


pytestmark = pytest.mark.real_api


@pytest.fixture(autouse=True)
def _skip_si_no_hay_credenciales() -> None:
    """Salta el test si las env vars criticas no estan presentes."""
    needed = ("EMAIL_ADDRESS", "EMAIL_APP_PASSWORD", "ANTHROPIC_API_KEY", "APP_SECRET_KEY")
    if not all(os.environ.get(k) for k in needed):
        get_settings.cache_clear()
        pytest.skip("Variables de email/app no presentes; saltando smoke real.")
    get_settings.cache_clear()


def test_send_y_list_contra_imap_smtp_reales() -> None:
    settings = get_settings()
    client = EmailClient(settings)

    # 1) Enviar un mensaje a nosotros mismos.
    subject = f"[SMOKE TEST] email_client.py {int(time.time())}"
    msg_id = client.send(
        to=settings.EMAIL_ADDRESS,
        subject=subject,
        body="Mensaje generado por la suite de pruebas. Puedes ignorarlo.",
    )
    assert msg_id.startswith("<") and msg_id.endswith(">")

    # 2) Listar no procesados — la conexion IMAP + la query funcionan.
    #    No comprobamos que nuestro mensaje recien enviado este ya en el listado
    #    porque Gmail puede tardar unos segundos en entregarlo.
    uids = client.list_unprocessed(max_results=5)
    assert isinstance(uids, list)
