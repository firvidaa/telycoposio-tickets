"""Tests del cliente IMAP/SMTP (``app.services.email_client``).

Sin red. Inyectamos factories doblados (``imap_factory``, ``smtp_factory``)
para sustituir ``imaplib.IMAP4_SSL`` y ``smtplib.SMTP``. Los doubles
replican solo la superficie minima que el cliente usa, asi los fallos del
cliente son detectables sin la fragilidad de un Mock generico.

Para los mensajes de prueba construimos RFC822 reales con
``email.message.EmailMessage`` y los serializamos a bytes — eso ejercita
el parser igual que un mensaje real de Gmail.
"""

from __future__ import annotations

import email.message
import logging
from typing import Any

import pytest

from app.config import Settings
from app.services.email_client import (
    PROCESSED_KEYWORD,
    AttachmentMeta,
    EmailClient,
    EmailFetchError,
    EmailStoreError,
    IncomingEmail,
)


# ---------------------------------------------------------------------------
# Settings de prueba
# ---------------------------------------------------------------------------


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        APP_SECRET_KEY="x" * 32,
        ANTHROPIC_API_KEY="sk-ant-test",
        EMAIL_ADDRESS="bot@example.com",
        EMAIL_APP_PASSWORD="abcdefghijklmnop",
        EMAIL_IMAP_HOST="imap.example.com",
        EMAIL_IMAP_PORT=993,
        EMAIL_SMTP_HOST="smtp.example.com",
        EMAIL_SMTP_PORT=587,
    )


# ---------------------------------------------------------------------------
# Doubles (mínima superficie del SDK)
# ---------------------------------------------------------------------------


class _FakeIMAP:
    def __init__(
        self,
        *,
        search_response: tuple[str, list[bytes]] = ("OK", [b""]),
        fetch_response: tuple[str, list[Any]] = ("OK", []),
        store_response: tuple[str, list[bytes]] = ("OK", [b""]),
    ) -> None:
        self.search_response = search_response
        self.fetch_response = fetch_response
        self.store_response = store_response
        self.calls: list[tuple[Any, ...]] = []

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.calls.append(("login", user, password))
        return ("OK", [b""])

    def select(self, mailbox: str) -> tuple[str, list[bytes]]:
        self.calls.append(("select", mailbox))
        return ("OK", [b"1"])

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        self.calls.append(("uid", command, *args))
        if command == "SEARCH":
            return self.search_response
        if command == "FETCH":
            return self.fetch_response
        if command == "STORE":
            return self.store_response
        return ("OK", [])

    def close(self) -> tuple[str, list[bytes]]:
        self.calls.append(("close",))
        return ("OK", [b""])

    def logout(self) -> tuple[str, list[bytes]]:
        self.calls.append(("logout",))
        return ("BYE", [b""])


class _FakeSMTP:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.sent: list[email.message.EmailMessage] = []

    def __enter__(self) -> "_FakeSMTP":
        return self

    def __exit__(self, *args: Any) -> None:
        self.calls.append(("__exit__",))

    def starttls(self, context: Any = None) -> None:
        self.calls.append(("starttls",))

    def login(self, user: str, password: str) -> None:
        self.calls.append(("login", user, password))

    def send_message(self, msg: email.message.EmailMessage) -> None:
        self.calls.append(("send_message",))
        self.sent.append(msg)


def _make_client(
    *,
    fake_imap: _FakeIMAP | None = None,
    fake_smtp: _FakeSMTP | None = None,
) -> tuple[EmailClient, _FakeIMAP, _FakeSMTP]:
    """Construye un EmailClient con factories que devuelven los fakes pasados."""
    if fake_imap is None:
        fake_imap = _FakeIMAP()
    if fake_smtp is None:
        fake_smtp = _FakeSMTP()
    client = EmailClient(
        _settings(),
        imap_factory=lambda *a, **kw: fake_imap,  # type: ignore[arg-type, return-value]
        smtp_factory=lambda *a, **kw: fake_smtp,  # type: ignore[arg-type, return-value]
    )
    return client, fake_imap, fake_smtp


# ---------------------------------------------------------------------------
# Helpers para construir mensajes RFC822 reales
# ---------------------------------------------------------------------------


def _rfc822_plain(
    *,
    from_addr: str = "Cliente <cliente@example.com>",
    subject: str = "Hola",
    body: str = "Cuerpo plano del mensaje.",
    message_id: str = "<abc@example.com>",
) -> bytes:
    msg = email.message.EmailMessage()
    msg["From"] = from_addr
    msg["To"] = "bot@example.com"
    msg["Subject"] = subject
    msg["Date"] = "Sun, 10 May 2026 10:00:00 +0000"
    msg["Message-Id"] = message_id
    msg.set_content(body)
    return bytes(msg)


def _rfc822_html_only(*, html: str = "<p>Hola <b>mundo</b></p>") -> bytes:
    msg = email.message.EmailMessage()
    msg["From"] = "Cliente <cliente@example.com>"
    msg["To"] = "bot@example.com"
    msg["Subject"] = "HTML only"
    msg["Date"] = "Sun, 10 May 2026 10:00:00 +0000"
    msg["Message-Id"] = "<html@example.com>"
    msg.set_content(html, subtype="html")
    return bytes(msg)


def _rfc822_alternative(*, plain: str = "VERSION PLANA", html: str = "<p>VERSION HTML</p>") -> bytes:
    msg = email.message.EmailMessage()
    msg["From"] = "Cliente <cliente@example.com>"
    msg["To"] = "bot@example.com"
    msg["Subject"] = "Alternative"
    msg["Date"] = "Sun, 10 May 2026 10:00:00 +0000"
    msg["Message-Id"] = "<alt@example.com>"
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")
    return bytes(msg)


def _rfc822_with_attachments(
    *,
    body: str = "Te adjunto los archivos.",
    attachments: list[tuple[str, bytes]] | None = None,
) -> bytes:
    msg = email.message.EmailMessage()
    msg["From"] = "Cliente <cliente@example.com>"
    msg["To"] = "bot@example.com"
    msg["Subject"] = "Con adjuntos"
    msg["Date"] = "Sun, 10 May 2026 10:00:00 +0000"
    msg["Message-Id"] = "<attach@example.com>"
    msg.set_content(body)
    for name, data in attachments or []:
        msg.add_attachment(
            data, maintype="application", subtype="octet-stream", filename=name
        )
    return bytes(msg)


def _fetch_response(raw: bytes) -> tuple[str, list[Any]]:
    """Empaqueta ``raw`` en la forma que devuelve ``imaplib.uid('FETCH', ...)``."""
    return ("OK", [(b"1 (UID 1 RFC822 {%d}" % len(raw), raw), b")"])


# ---------------------------------------------------------------------------
# list_unprocessed
# ---------------------------------------------------------------------------


def test_list_unprocessed_devuelve_uids() -> None:
    fake = _FakeIMAP(search_response=("OK", [b"1 5 7 9"]))
    client, _, _ = _make_client(fake_imap=fake)
    uids = client.list_unprocessed()
    assert uids == ["1", "5", "7", "9"]


def test_list_unprocessed_inbox_vacio() -> None:
    fake = _FakeIMAP(search_response=("OK", [b""]))
    client, _, _ = _make_client(fake_imap=fake)
    assert client.list_unprocessed() == []


def test_list_unprocessed_respeta_max_results() -> None:
    fake = _FakeIMAP(search_response=("OK", [b"1 2 3 4 5 6 7 8 9 10"]))
    client, _, _ = _make_client(fake_imap=fake)
    assert client.list_unprocessed(max_results=3) == ["1", "2", "3"]


def test_list_unprocessed_pasa_query_correcta_imap() -> None:
    """SEARCH UNDELETED NOT KEYWORD TLY_PROCESSED — la unica forma robusta y portable."""
    fake = _FakeIMAP(search_response=("OK", [b"1"]))
    client, _, _ = _make_client(fake_imap=fake)
    client.list_unprocessed()
    # Encuentra la llamada SEARCH y verifica los argumentos.
    search_calls = [c for c in fake.calls if c[0] == "uid" and c[1] == "SEARCH"]
    assert len(search_calls) == 1
    _, _, *args = search_calls[0]
    # args = (None, "UNDELETED", "NOT", "KEYWORD", "TLY_PROCESSED")
    assert "UNDELETED" in args
    assert "NOT" in args
    assert "KEYWORD" in args
    assert PROCESSED_KEYWORD in args


def test_list_unprocessed_search_fallido_devuelve_lista_vacia() -> None:
    fake = _FakeIMAP(search_response=("NO", [b"err"]))
    client, _, _ = _make_client(fake_imap=fake)
    assert client.list_unprocessed() == []


def test_list_unprocessed_login_y_logout_se_llaman() -> None:
    fake = _FakeIMAP(search_response=("OK", [b"1"]))
    client, _, _ = _make_client(fake_imap=fake)
    client.list_unprocessed()
    ops = [c[0] for c in fake.calls]
    assert "login" in ops
    assert "logout" in ops


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


def test_get_parsea_text_plain() -> None:
    raw = _rfc822_plain(body="Hola mundo")
    fake = _FakeIMAP(fetch_response=_fetch_response(raw))
    client, _, _ = _make_client(fake_imap=fake)
    res = client.get("1")
    assert isinstance(res, IncomingEmail)
    assert res.body == "Hola mundo"
    assert res.body_source == "text/plain"


def test_get_parsea_html_solo_y_lo_convierte_a_texto() -> None:
    raw = _rfc822_html_only(html="<p>Hola <b>mundo</b></p>")
    fake = _FakeIMAP(fetch_response=_fetch_response(raw))
    client, _, _ = _make_client(fake_imap=fake)
    res = client.get("1")
    assert res.body_source == "html_converted"
    assert "Hola" in res.body and "mundo" in res.body
    assert "<b>" not in res.body  # tags eliminados por BeautifulSoup


def test_get_multipart_prefiere_text_plain_sobre_html() -> None:
    raw = _rfc822_alternative(plain="VERSION PLANA", html="<p>VERSION HTML</p>")
    fake = _FakeIMAP(fetch_response=_fetch_response(raw))
    client, _, _ = _make_client(fake_imap=fake)
    res = client.get("1")
    assert res.body_source == "text/plain"
    assert "VERSION PLANA" in res.body
    assert "VERSION HTML" not in res.body


def test_get_extrae_adjuntos_con_size_bytes() -> None:
    raw = _rfc822_with_attachments(
        attachments=[
            ("captura.png", b"\x89PNG\r\n\x1a\n" + b"x" * 100),
            ("log.txt", b"linea1\nlinea2\n"),
        ]
    )
    fake = _FakeIMAP(fetch_response=_fetch_response(raw))
    client, _, _ = _make_client(fake_imap=fake)
    res = client.get("1")
    assert len(res.attachments) == 2
    nombres = {a.name for a in res.attachments}
    assert nombres == {"captura.png", "log.txt"}
    a_log = next(a for a in res.attachments if a.name == "log.txt")
    assert a_log == AttachmentMeta(name="log.txt", size_bytes=14)


def test_get_decodifica_subject_rfc2047() -> None:
    msg = email.message.EmailMessage()
    msg["From"] = "Cliente <cliente@example.com>"
    msg["To"] = "bot@example.com"
    # Header con encoded-words (=?utf-8?b?...?=) se genera al setear texto con caracteres no-ASCII.
    msg["Subject"] = "Solicitud de presupuesto: telefonia"  # ASCII-safe
    msg["Date"] = "Sun, 10 May 2026 10:00:00 +0000"
    msg["Message-Id"] = "<x@example.com>"
    msg.set_content("...")
    # Reemplazamos el subject por encoded-words explicitos.
    raw_str = bytes(msg).decode("ascii", errors="replace")
    raw_str = raw_str.replace(
        "Subject: Solicitud de presupuesto: telefonia",
        "Subject: =?utf-8?b?U29saWNpdHVkIGRlIHByZXN1cHVlc3Rv?=",  # "Solicitud de presupuesto" en b64
    )
    raw = raw_str.encode("ascii", errors="replace")
    fake = _FakeIMAP(fetch_response=_fetch_response(raw))
    client, _, _ = _make_client(fake_imap=fake)
    res = client.get("1")
    assert res.subject == "Solicitud de presupuesto"


def test_get_extrae_from_name_y_email() -> None:
    raw = _rfc822_plain(from_addr='"Maria Garcia" <maria@example.com>')
    fake = _FakeIMAP(fetch_response=_fetch_response(raw))
    client, _, _ = _make_client(fake_imap=fake)
    res = client.get("1")
    assert res.from_name == "Maria Garcia"
    assert res.from_email == "maria@example.com"


def test_get_devuelve_message_id_completo() -> None:
    raw = _rfc822_plain(message_id="<abc-12345@mail.example.com>")
    fake = _FakeIMAP(fetch_response=_fetch_response(raw))
    client, _, _ = _make_client(fake_imap=fake)
    res = client.get("1")
    assert res.rfc822_message_id == "<abc-12345@mail.example.com>"


def test_get_loguea_source_y_metricas(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.services.email_client")
    raw = _rfc822_plain(body="Cuerpo de prueba con varias palabras")
    fake = _FakeIMAP(fetch_response=_fetch_response(raw))
    client, _, _ = _make_client(fake_imap=fake)
    client.get("42")
    assert "uid=42" in caplog.text
    assert "source=text/plain" in caplog.text
    assert "body_len=" in caplog.text
    assert "attachments=0" in caplog.text


def test_get_loguea_html_converted_cuando_solo_html(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="app.services.email_client")
    raw = _rfc822_html_only()
    fake = _FakeIMAP(fetch_response=_fetch_response(raw))
    client, _, _ = _make_client(fake_imap=fake)
    client.get("99")
    assert "source=html_converted" in caplog.text


def test_get_si_fetch_falla_levanta() -> None:
    fake = _FakeIMAP(fetch_response=("NO", [b"err"]))
    client, _, _ = _make_client(fake_imap=fake)
    with pytest.raises(EmailFetchError):
        client.get("1")


def test_get_si_respuesta_vacia_levanta() -> None:
    fake = _FakeIMAP(fetch_response=("OK", []))
    client, _, _ = _make_client(fake_imap=fake)
    with pytest.raises(EmailFetchError):
        client.get("1")


# ---------------------------------------------------------------------------
# mark_processed
# ---------------------------------------------------------------------------


def test_mark_processed_invoca_store_con_keyword() -> None:
    fake = _FakeIMAP()
    client, _, _ = _make_client(fake_imap=fake)
    client.mark_processed("42")
    store_calls = [c for c in fake.calls if c[0] == "uid" and c[1] == "STORE"]
    assert len(store_calls) == 1
    _, _, uid, op, keyword = store_calls[0]
    assert uid == "42"
    assert op == "+FLAGS"
    assert keyword == PROCESSED_KEYWORD


def test_mark_processed_si_store_falla_levanta() -> None:
    fake = _FakeIMAP(store_response=("NO", [b"err"]))
    client, _, _ = _make_client(fake_imap=fake)
    with pytest.raises(EmailStoreError):
        client.mark_processed("42")


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------


def test_send_construye_headers_minimos() -> None:
    client, _, fake_smtp = _make_client()
    msg_id = client.send(
        to="cliente@example.com",
        subject="Confirmacion",
        body="Tu ticket es TLY-2026-0042.",
    )
    assert len(fake_smtp.sent) == 1
    sent = fake_smtp.sent[0]
    assert sent["From"] == "bot@example.com"
    assert sent["To"] == "cliente@example.com"
    assert sent["Subject"] == "Confirmacion"
    assert sent["Date"] is not None
    assert sent["Message-Id"] == msg_id
    assert sent.get_content().strip() == "Tu ticket es TLY-2026-0042."


def test_send_invoca_starttls_y_login_en_orden() -> None:
    client, _, fake_smtp = _make_client()
    client.send(to="x@example.com", subject="s", body="b")
    ops = [c[0] for c in fake_smtp.calls]
    assert ops.index("starttls") < ops.index("login") < ops.index("send_message")


def test_send_login_usa_app_password() -> None:
    client, _, fake_smtp = _make_client()
    client.send(to="x@example.com", subject="s", body="b")
    login_call = next(c for c in fake_smtp.calls if c[0] == "login")
    _, user, password = login_call
    assert user == "bot@example.com"
    assert password == "abcdefghijklmnop"


def test_send_con_in_reply_to_anyade_headers() -> None:
    client, _, fake_smtp = _make_client()
    client.send(
        to="x@example.com",
        subject="Re: hola",
        body="b",
        in_reply_to="<original-id@example.com>",
    )
    sent = fake_smtp.sent[0]
    assert sent["In-Reply-To"] == "<original-id@example.com>"
    assert sent["References"] == "<original-id@example.com>"


def test_send_normaliza_in_reply_to_sin_corchetes() -> None:
    """Si el llamante pasa un Message-Id sin <>, lo envolvemos."""
    client, _, fake_smtp = _make_client()
    client.send(
        to="x@example.com",
        subject="Re: hola",
        body="b",
        in_reply_to="original-id@example.com",
    )
    sent = fake_smtp.sent[0]
    assert sent["In-Reply-To"] == "<original-id@example.com>"


def test_send_devuelve_message_id_generado() -> None:
    client, _, _ = _make_client()
    msg_id = client.send(to="x@example.com", subject="s", body="b")
    assert msg_id.startswith("<") and msg_id.endswith(">")
    assert "example.com" in msg_id  # deriva del dominio del From


# ---------------------------------------------------------------------------
# Saneo de headers (defensa contra CR/LF embebido)
# ---------------------------------------------------------------------------


def test_sanitize_header_colapsa_crlf_en_espacio() -> None:
    """``_sanitize_header`` aplana CR/LF y combinaciones en un solo espacio."""
    from app.services.email_client import _sanitize_header

    # CRLF tipico (folding de subject en MIME) sin WSP siguiente.
    assert _sanitize_header("foo\r\nbar") == "foo bar"
    # Multiple CR/LF/TAB residuales se colapsan en un solo espacio.
    assert _sanitize_header("foo\r\n\r\n\tbar") == "foo bar"
    # CR solo, LF solo.
    assert _sanitize_header("foo\rbar") == "foo bar"
    assert _sanitize_header("foo\nbar") == "foo bar"


def test_sanitize_header_unfold_rfc5322_no_dobla_espacios() -> None:
    """``\\r\\n`` + WSP es continuacion de header (RFC 5322); el unfold
    debe dejar UN solo espacio, no dos. Caso real: el subject de
    notificaciones de GitHub viene foldeado.
    """
    from app.services.email_client import _sanitize_header

    assert _sanitize_header("foo\r\n bar") == "foo bar"
    assert _sanitize_header("foo\n\tbar") == "foo bar"
    assert _sanitize_header("foo\r\n   bar") == "foo bar"


def test_sanitize_header_trim_extremos() -> None:
    from app.services.email_client import _sanitize_header

    assert _sanitize_header("  hola  ") == "hola"
    assert _sanitize_header("\r\nhola\r\n") == "hola"


def test_sanitize_header_no_modifica_texto_normal() -> None:
    from app.services.email_client import _sanitize_header

    assert _sanitize_header("Asunto normal") == "Asunto normal"
    assert _sanitize_header("Re: [TICKET-123] Hola") == "Re: [TICKET-123] Hola"


def test_sanitize_header_vacio_pasa_tal_cual() -> None:
    from app.services.email_client import _sanitize_header

    assert _sanitize_header("") == ""


def test_send_con_subject_que_contiene_crlf_no_lanza_y_se_sanea() -> None:
    """Regresion: GitHub manda subjects con ``\\r\\n`` embebido y antes
    del fix el EmailMessage lanzaba ``ValueError`` al asignar el header.
    Ahora el subject se sanea y el envio prospera.
    """
    client, _, fake_smtp = _make_client()
    client.send(
        to="cliente@example.com",
        subject="[GitHub] OAuth added to your\r\n account",
        body="Confirmacion",
    )
    sent = fake_smtp.sent[0]
    # CRLF eliminado, queda una sola linea.
    assert sent["Subject"] == "[GitHub] OAuth added to your account"
    assert "\r" not in sent["Subject"]
    assert "\n" not in sent["Subject"]


def test_send_con_to_que_contiene_crlf_no_inyecta_bcc() -> None:
    """Defensa en profundidad: aunque ``to`` viene parseado de
    ``IncomingEmail``, saneamos por si en el futuro entra desde otro
    origen. La propiedad que importa: no se inyecta ningun header
    nuevo (Bcc, Cc, etc.) y el envio no lanza.
    """
    client, _, fake_smtp = _make_client()
    client.send(
        to="cliente@example.com\r\nBcc: atacante@evil.com",
        subject="x",
        body="b",
    )
    sent = fake_smtp.sent[0]
    # Lo importante: NO hay Bcc inyectado como header separado.
    assert sent["Bcc"] is None
    # El To no contiene CR/LF embebido (sea cual sea la normalizacion
    # exacta que aplique la policy).
    assert "\r" not in str(sent["To"])
    assert "\n" not in str(sent["To"])
