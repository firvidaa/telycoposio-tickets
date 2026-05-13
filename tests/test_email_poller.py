"""Tests del worker de polling (``app.workers.email_poller``).

Estrategia:

- BD SQLite en disco temporal (``tmp_path``) con esquema inicializado.
  Como el ``EmailPoller`` abre y cierra la conexion el mismo, usamos un
  fichero (no ``:memory:``) para que la conexion del test pueda hacer
  ``SELECT`` despues de ``run_once``.

- Cliente IMAP/SMTP doblado por ``_FakeEmailClient`` — replica solo la
  superficie minima que el poller usa (``list_unprocessed``, ``get``,
  ``send``, ``mark_processed``) con hooks para inyectar errores.

- Clasificador inyectado como callable. Default = ``_classify_admin``
  (devuelve ADMINISTRATIVO con alta confianza). Tests que quieran cubrir
  ``needs_review`` pasan ``ClassificationResult.failed()``.

- Reloj congelado en 2026-05-12 12:00 UTC salvo que el test lo cambie.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.db.sqlite import get_connection, init_schema
from app.models.ticket import TicketCategory
from app.services.classifier import ClassificationResult
from app.services.email_client import (
    AttachmentMeta,
    EmailClient,
    EmailFetchError,
    EmailStoreError,
    IncomingEmail,
)
from app.services.ticket_service import get_ticket
from app.workers.email_poller import (
    CONFIRMATION_BODY_TEMPLATE,
    CONFIRMATION_SUBJECT_TEMPLATE,
    INTERNAL_NOTIFICATION_BODY_TEMPLATE,
    INTERNAL_NOTIFICATION_SUBJECT_TEMPLATE,
    MAX_FAILURES_PER_UID,
    EmailPoller,
)


# ---------------------------------------------------------------------------
# Fixtures y helpers
# ---------------------------------------------------------------------------


_FROZEN_NOW = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)


def _frozen_now() -> datetime:
    return _FROZEN_NOW


def _make_settings(
    db_path: Path,
    *,
    internal_email: str | None = "soporte@telycoposio.com",
    base_url: str = "http://localhost:8000",
) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        APP_SECRET_KEY="x" * 32,
        ANTHROPIC_API_KEY="sk-ant-test",
        EMAIL_ADDRESS="bot@example.com",
        EMAIL_APP_PASSWORD="abcdefghijklmnop",
        SQLITE_PATH=str(db_path),
        INTERNAL_NOTIFICATION_EMAIL=internal_email,
        APP_BASE_URL=base_url,
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """SQLite en fichero temporal con el esquema cargado."""
    p = tmp_path / "test.db"
    with get_connection(str(p)) as conn:
        init_schema(conn)
    return p


@pytest.fixture
def db_conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Conexion para que el test inspeccione el estado de la BD."""
    with get_connection(str(db_path)) as conn:
        yield conn


# ---------------------------------------------------------------------------
# Double del EmailClient
# ---------------------------------------------------------------------------


class _SendCall:
    """Captura los kwargs de una llamada a ``send``."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    def __repr__(self) -> str:
        to = self.kwargs.get("to")
        subj = self.kwargs.get("subject")
        return f"_SendCall(to={to!r}, subject={subj!r})"


class _FakeEmailClient:
    """Double con la misma superficie que ``EmailClient`` usa el poller.

    Hooks de inyeccion de errores:

    - ``get_errors_by_uid``: dict ``{uid: Exception}`` — si el UID esta en
      el dict, ``get(uid)`` lanza esa excepcion.
    - ``send_errors``: lista de excepciones a lanzar en orden en cada
      llamada a ``send`` (``[]`` -> nunca falla). ``None`` deja pasar.
    - ``mark_errors_by_uid``: dict ``{uid: Exception}``.
    """

    def __init__(
        self,
        *,
        unprocessed_uids: list[str] | None = None,
        emails_by_uid: dict[str, IncomingEmail] | None = None,
        get_errors_by_uid: dict[str, Exception] | None = None,
        send_errors: list[Exception | None] | None = None,
        mark_errors_by_uid: dict[str, Exception] | None = None,
    ) -> None:
        self._unprocessed_uids = list(unprocessed_uids or [])
        self._emails_by_uid = emails_by_uid or {}
        self._get_errors = get_errors_by_uid or {}
        self._send_errors = list(send_errors or [])
        self._mark_errors = mark_errors_by_uid or {}

        self.list_calls = 0
        self.get_calls: list[str] = []
        self.send_calls: list[_SendCall] = []
        self.mark_calls: list[str] = []

    def list_unprocessed(self, *, max_results: int = 20) -> list[str]:
        self.list_calls += 1
        # Filtrar los ya marcados (asi simulamos un INBOX real entre
        # iteraciones cuando un test corre run_once dos veces).
        live = [u for u in self._unprocessed_uids if u not in self.mark_calls]
        return live[:max_results]

    def get(self, uid: str) -> IncomingEmail:
        self.get_calls.append(uid)
        if uid in self._get_errors:
            raise self._get_errors[uid]
        if uid not in self._emails_by_uid:
            raise EmailFetchError(f"FAKE: uid {uid} no preparado en el fake")
        return self._emails_by_uid[uid]

    def send(self, **kwargs: Any) -> str:
        self.send_calls.append(_SendCall(**kwargs))
        if self._send_errors:
            exc = self._send_errors.pop(0)
            if exc is not None:
                raise exc
        return "<fake-message-id@example.com>"

    def mark_processed(self, uid: str) -> None:
        if uid in self._mark_errors:
            # Importante: la llamada NO se registra si lanza, asi el filtro
            # de "ya marcados" en list_unprocessed sigue sirviendo en el
            # siguiente ciclo.
            raise self._mark_errors[uid]
        self.mark_calls.append(uid)


# ---------------------------------------------------------------------------
# Builders de IncomingEmail
# ---------------------------------------------------------------------------


def _incoming(
    uid: str,
    *,
    subject: str = "Mi router no da senyal",
    body: str = "Hola, no me funciona el router 4G desde ayer.",
    from_name: str | None = "Maria Garcia",
    from_email: str | None = "maria@ejemplo.com",
    rfc822_message_id: str | None = None,
    attachments: list[AttachmentMeta] | None = None,
) -> IncomingEmail:
    if rfc822_message_id is None:
        rfc822_message_id = f"<msg-{uid}@ejemplo.com>"
    return IncomingEmail(
        uid=uid,
        rfc822_message_id=rfc822_message_id,
        from_name=from_name,
        from_email=from_email,
        subject=subject,
        body=body,
        body_source="text/plain",
        received_at=_FROZEN_NOW,
        attachments=attachments or [],
    )


# ---------------------------------------------------------------------------
# Classifier fakes
# ---------------------------------------------------------------------------


def _classify_admin(subject: str, body: str) -> ClassificationResult:
    return ClassificationResult(
        category=TicketCategory.ADMINISTRATIVO,
        confidence=0.92,
        reasoning="Mencion explicita a factura/contrato.",
    )


def _classify_low_confidence(subject: str, body: str) -> ClassificationResult:
    return ClassificationResult(
        category=TicketCategory.COMERCIAL,
        confidence=0.4,
        reasoning="Mensaje ambiguo, podria ser SOPORTE.",
    )


def _classify_failed(subject: str, body: str) -> ClassificationResult:
    return ClassificationResult.failed()


def _make_poller(
    db_path: Path,
    client: _FakeEmailClient,
    *,
    classify_fn: Any = _classify_admin,
    internal_email: str | None = "soporte@telycoposio.com",
    base_url: str = "http://localhost:8000",
    now: datetime | None = None,
) -> EmailPoller:
    return EmailPoller(
        email_client=client,  # type: ignore[arg-type]
        settings=_make_settings(db_path, internal_email=internal_email, base_url=base_url),
        classify_fn=classify_fn,
        now_fn=(lambda: now) if now else _frozen_now,
    )


# ---------------------------------------------------------------------------
# Tests: run_once
# ---------------------------------------------------------------------------


def test_run_once_inbox_vacio_devuelve_cero(db_path: Path) -> None:
    client = _FakeEmailClient(unprocessed_uids=[])
    poller = _make_poller(db_path, client)
    assert poller.run_once() == 0
    assert client.send_calls == []
    assert client.mark_calls == []


def test_run_once_email_normal_crea_ticket_y_marca(
    db_path: Path, db_conn: sqlite3.Connection
) -> None:
    email = _incoming("1")
    client = _FakeEmailClient(
        unprocessed_uids=["1"], emails_by_uid={"1": email}
    )
    poller = _make_poller(db_path, client)

    assert poller.run_once() == 1

    # Confirmacion + notif interna enviadas, UID marcado.
    assert len(client.send_calls) == 2
    assert client.mark_calls == ["1"]

    # Ticket en BD con datos correctos.
    row = db_conn.execute("SELECT * FROM tickets").fetchone()
    assert row is not None
    assert row["from_email"] == "maria@ejemplo.com"
    assert row["category"] == "ADMINISTRATIVO"
    assert row["category_confidence"] == 0.92
    assert row["needs_review"] == 0
    assert row["raw_message_id"] == "<msg-1@ejemplo.com>"
    assert row["client_notified_at"] is not None


def test_run_once_confirmacion_al_cliente_usa_plantillas(
    db_path: Path,
) -> None:
    email = _incoming("1", from_name="Juan Perez", subject="No tengo internet")
    client = _FakeEmailClient(unprocessed_uids=["1"], emails_by_uid={"1": email})
    poller = _make_poller(db_path, client)
    poller.run_once()

    # Primera llamada a send: confirmacion al cliente.
    confirm = client.send_calls[0].kwargs
    assert confirm["to"] == "maria@ejemplo.com"  # from_email del email
    # Subject construido con la plantilla aprobada.
    assert "No tengo internet" in confirm["subject"]
    assert "TLY-2026-0001" in confirm["subject"]
    assert confirm["subject"].startswith("Re: ")
    # Body con el saludo personalizado y el numero de referencia.
    assert "Hola Juan Perez," in confirm["body"]
    assert "Tu numero de referencia es" in confirm["body"]
    assert "TLY-2026-0001" in confirm["body"]
    assert "Equipo Telycoposio" in confirm["body"]
    # Aviso RGPD presente.
    assert "Aviso de privacidad" in confirm["body"]
    # in_reply_to propagado para hilar la conversacion en Gmail.
    assert confirm["in_reply_to"] == "<msg-1@ejemplo.com>"


def test_run_once_saludo_generico_si_no_hay_from_name(db_path: Path) -> None:
    email = _incoming("1", from_name=None)
    client = _FakeEmailClient(unprocessed_uids=["1"], emails_by_uid={"1": email})
    poller = _make_poller(db_path, client)
    poller.run_once()

    confirm = client.send_calls[0].kwargs
    # Sin nombre, el saludo termina con coma directamente despues de "Hola".
    assert confirm["body"].startswith("Hola,\n")


def test_run_once_notif_interna_incluye_link_clasificacion_y_preview(
    db_path: Path,
) -> None:
    email = _incoming(
        "1",
        subject="Asunto largo " + "x" * 200,  # forzara truncado
        body="cuerpo " * 100,  # ~700 chars: forzara truncado a 500
    )
    client = _FakeEmailClient(unprocessed_uids=["1"], emails_by_uid={"1": email})
    poller = _make_poller(db_path, client, base_url="https://tickets.example.com")
    poller.run_once()

    internal = client.send_calls[1].kwargs
    assert internal["to"] == "soporte@telycoposio.com"
    # Subject truncado pero con el ticket id visible.
    assert internal["subject"].startswith("[Ticket TLY-2026-0001]")
    # Body con categoria + confianza + razonamiento.
    assert "ADMINISTRATIVO" in internal["body"]
    assert "92%" in internal["body"]
    assert "factura/contrato" in internal["body"]
    # Link clickable con la base url configurada.
    assert "https://tickets.example.com/tickets/TLY-2026-0001" in internal["body"]
    # Preview cortado a 500 chars (mas el contexto de la plantilla).
    # Solo verificamos que el body no contiene todo el original.
    assert len(internal["body"]) < len(email.body) + 1000


def test_run_once_notif_interna_categoria_none_si_clasificador_fallo(
    db_path: Path,
) -> None:
    email = _incoming("1")
    client = _FakeEmailClient(unprocessed_uids=["1"], emails_by_uid={"1": email})
    poller = _make_poller(db_path, client, classify_fn=_classify_failed)
    poller.run_once()

    internal = client.send_calls[1].kwargs
    assert "(sin clasificar - needs_review)" in internal["body"]
    assert "confianza n/a" in internal["body"]


def test_run_once_no_envia_notif_interna_sin_destinatario(db_path: Path) -> None:
    email = _incoming("1")
    client = _FakeEmailClient(unprocessed_uids=["1"], emails_by_uid={"1": email})
    poller = _make_poller(db_path, client, internal_email=None)
    poller.run_once()

    # Solo se envia la confirmacion al cliente.
    assert len(client.send_calls) == 1
    assert client.send_calls[0].kwargs["to"] == "maria@ejemplo.com"


def test_run_once_duplicado_por_raw_message_id_no_reenvia_confirmacion(
    db_path: Path, db_conn: sqlite3.Connection
) -> None:
    """Primer run crea ticket + notifica; segundo run con mismo Message-Id
    encuentra el ticket existente con client_notified_at != NULL y NO
    reenvia la confirmacion."""
    email = _incoming("1", rfc822_message_id="<dupe@ejemplo.com>")
    client = _FakeEmailClient(unprocessed_uids=["1"], emails_by_uid={"1": email})
    poller = _make_poller(db_path, client)

    poller.run_once()
    # Llega un email "nuevo" con el mismo Message-Id (caso real: alguien
    # reenvia el mismo mensaje, o un fallo de marcado deja el original).
    second_email = _incoming(
        "99", rfc822_message_id="<dupe@ejemplo.com>", subject="Reenviado"
    )
    client._unprocessed_uids = ["99"]
    client._emails_by_uid = {"99": second_email}
    client.send_calls.clear()  # ignoramos las del primer run

    poller.run_once()

    # Segundo run: NO se llama send (ni cliente ni interna), pero SI se
    # marca procesado el UID 99 para sacarlo del bucle.
    assert client.send_calls == []
    assert "99" in client.mark_calls

    # Solo hay un ticket en BD.
    rows = db_conn.execute("SELECT id FROM tickets").fetchall()
    assert len(rows) == 1


def test_run_once_clasificador_fallo_crea_ticket_con_needs_review(
    db_path: Path, db_conn: sqlite3.Connection
) -> None:
    email = _incoming("1")
    client = _FakeEmailClient(unprocessed_uids=["1"], emails_by_uid={"1": email})
    poller = _make_poller(db_path, client, classify_fn=_classify_failed)

    assert poller.run_once() == 1
    row = db_conn.execute("SELECT * FROM tickets").fetchone()
    assert row["category"] is None
    assert row["needs_review"] == 1


def test_run_once_clasificador_baja_confianza_marca_needs_review(
    db_path: Path, db_conn: sqlite3.Connection
) -> None:
    email = _incoming("1")
    client = _FakeEmailClient(unprocessed_uids=["1"], emails_by_uid={"1": email})
    poller = _make_poller(db_path, client, classify_fn=_classify_low_confidence)
    poller.run_once()

    row = db_conn.execute("SELECT * FROM tickets").fetchone()
    assert row["category"] == "COMERCIAL"
    assert row["category_confidence"] == 0.4
    assert row["needs_review"] == 1


def test_run_once_send_confirmacion_falla_no_marca_y_cuenta_fallo(
    db_path: Path, db_conn: sqlite3.Connection
) -> None:
    email = _incoming("1")
    client = _FakeEmailClient(
        unprocessed_uids=["1"],
        emails_by_uid={"1": email},
        send_errors=[RuntimeError("smtp down")],
    )
    poller = _make_poller(db_path, client)

    assert poller.run_once() == 0
    # Ticket CREADO (la BD ya tiene la fila) pero NO marcado ni notif interna.
    rows = db_conn.execute("SELECT * FROM tickets").fetchall()
    assert len(rows) == 1
    assert rows[0]["client_notified_at"] is None
    # mark_processed NO se llamo (el UID volvera a salir en el proximo ciclo).
    assert client.mark_calls == []
    # Notif interna NO se llamo (solo se envia tras confirmacion exitosa).
    assert len(client.send_calls) == 1
    # Contador incrementado.
    assert poller._failure_counts["1"] == 1


def test_run_once_send_interno_falla_pero_si_marca_procesado(
    db_path: Path, db_conn: sqlite3.Connection
) -> None:
    """Notif interna best-effort: un fallo aqui NO impide marcar procesado
    ni cuenta como fallo del UID."""
    email = _incoming("1")
    client = _FakeEmailClient(
        unprocessed_uids=["1"],
        emails_by_uid={"1": email},
        # 1a llamada: confirmacion OK; 2a llamada (interna): falla.
        send_errors=[None, RuntimeError("smtp interno hipoteticamente caido")],
    )
    poller = _make_poller(db_path, client)

    assert poller.run_once() == 1
    assert client.mark_calls == ["1"]
    # Ticket con client_notified_at puesto (la confirmacion si llego).
    row = db_conn.execute("SELECT client_notified_at FROM tickets").fetchone()
    assert row["client_notified_at"] is not None
    assert poller._failure_counts == {}  # exito limpia el contador


def test_run_once_mark_processed_falla_cuenta_como_fallo(db_path: Path) -> None:
    email = _incoming("1")
    client = _FakeEmailClient(
        unprocessed_uids=["1"],
        emails_by_uid={"1": email},
        mark_errors_by_uid={"1": EmailStoreError("imap rechaza el STORE")},
    )
    poller = _make_poller(db_path, client)

    assert poller.run_once() == 0
    assert poller._failure_counts["1"] == 1


def test_run_once_fetch_falla_cuenta_como_fallo(db_path: Path) -> None:
    client = _FakeEmailClient(
        unprocessed_uids=["1"],
        get_errors_by_uid={"1": EmailFetchError("fetch boom")},
    )
    poller = _make_poller(db_path, client)
    assert poller.run_once() == 0
    assert poller._failure_counts["1"] == 1
    # Sin ticket ni envios.
    assert client.send_calls == []


def test_run_once_max_failures_marca_uid_y_loguea_error(
    db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Tras MAX_FAILURES fallos consecutivos en el mismo UID, lo marcamos
    procesado para sacarlo del bucle y logueamos ERROR ruidoso."""
    caplog.set_level(logging.WARNING, logger="app.workers.email_poller")
    client = _FakeEmailClient(
        unprocessed_uids=["7"],
        get_errors_by_uid={"7": EmailFetchError("siempre falla")},
    )
    poller = _make_poller(db_path, client)

    for _ in range(MAX_FAILURES_PER_UID):
        poller.run_once()

    assert poller._failure_counts["7"] == MAX_FAILURES_PER_UID
    # Se llamo mark_processed al llegar al cap.
    assert "7" in client.mark_calls
    # ERROR ruidoso visible en logs.
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("POLLER UID toxico" in r.message for r in error_records)


def test_run_once_uid_muerto_se_filtra_en_siguientes_iteraciones(
    db_path: Path,
) -> None:
    """Aunque el UID siga apareciendo en list_unprocessed, una vez muerto
    en memoria no se vuelve a procesar."""
    # Simulamos un cliente cuyo mark_processed FALLA tambien — el UID
    # seguira en list_unprocessed pero el contador en memoria lo bloquea.
    client = _FakeEmailClient(
        unprocessed_uids=["9"],
        get_errors_by_uid={"9": EmailFetchError("boom")},
        mark_errors_by_uid={"9": EmailStoreError("store falla")},
    )
    poller = _make_poller(db_path, client)

    for _ in range(MAX_FAILURES_PER_UID):
        poller.run_once()

    get_calls_at_cap = len(client.get_calls)
    # Un run extra: el UID muerto se filtra, ya no se intenta get().
    poller.run_once()
    assert len(client.get_calls) == get_calls_at_cap


def test_run_once_sin_from_email_no_envia_pero_marca(
    db_path: Path, db_conn: sqlite3.Connection
) -> None:
    """Caso degenerado: email sin direccion de retorno. Ticket creado sin
    confirmacion (client_notified_at NULL), no entra al bucle de fallos."""
    email = _incoming("1", from_email=None, from_name=None)
    client = _FakeEmailClient(unprocessed_uids=["1"], emails_by_uid={"1": email})
    poller = _make_poller(db_path, client)

    assert poller.run_once() == 1
    row = db_conn.execute("SELECT * FROM tickets").fetchone()
    assert row is not None
    assert row["client_notified_at"] is None
    assert client.send_calls == []  # no podemos enviar nada
    assert client.mark_calls == ["1"]  # sacado del bucle


def test_run_once_attachments_propagados_al_ticket(
    db_path: Path, db_conn: sqlite3.Connection
) -> None:
    email = _incoming(
        "1",
        attachments=[
            AttachmentMeta(name="factura.pdf", size_bytes=12345),
            AttachmentMeta(name="captura.png", size_bytes=678),
        ],
    )
    client = _FakeEmailClient(unprocessed_uids=["1"], emails_by_uid={"1": email})
    poller = _make_poller(db_path, client)
    poller.run_once()

    import json

    row = db_conn.execute("SELECT attachments FROM tickets").fetchone()
    parsed = json.loads(row["attachments"])
    assert len(parsed) == 2
    nombres = {a["name"] for a in parsed}
    assert nombres == {"factura.pdf", "captura.png"}


def test_run_once_list_unprocessed_falla_devuelve_cero(
    db_path: Path,
) -> None:
    """Si IMAP esta caido la lista no llega; el scheduler reintentara solo."""

    class _BoomClient(_FakeEmailClient):
        def list_unprocessed(self, *, max_results: int = 20) -> list[str]:
            raise EmailFetchError("imap caido")

    client = _BoomClient()
    poller = _make_poller(db_path, client)
    assert poller.run_once() == 0


def test_run_once_un_email_fallido_no_para_los_demas(
    db_path: Path, db_conn: sqlite3.Connection
) -> None:
    """Si un email rompe, los siguientes se procesan normalmente."""
    email_ok = _incoming("2", subject="Bien")
    client = _FakeEmailClient(
        unprocessed_uids=["1", "2"],
        emails_by_uid={"2": email_ok},
        get_errors_by_uid={"1": EmailFetchError("uno roto")},
    )
    poller = _make_poller(db_path, client)

    assert poller.run_once() == 1
    # El UID 2 produjo ticket y se marco.
    assert "2" in client.mark_calls
    # El UID 1 incremento contador, no se marco.
    assert "1" not in client.mark_calls
    assert poller._failure_counts.get("1") == 1


def test_run_once_dos_ciclos_seguidos_no_duplican_ticket(
    db_path: Path, db_conn: sqlite3.Connection
) -> None:
    """Llamar run_once dos veces (igual que hara el scheduler) no crea
    duplicados ni reenvia confirmaciones de los emails ya procesados."""
    e1 = _incoming("1")
    client = _FakeEmailClient(unprocessed_uids=["1"], emails_by_uid={"1": e1})
    poller = _make_poller(db_path, client)

    poller.run_once()
    poller.run_once()

    # Solo un ticket, solo una confirmacion + una notif interna.
    rows = db_conn.execute("SELECT id FROM tickets").fetchall()
    assert len(rows) == 1
    assert len(client.send_calls) == 2
    assert client.mark_calls == ["1"]
