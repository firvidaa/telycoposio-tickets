"""Cliente de email: IMAP (lectura + marcar procesado) + SMTP (envio).

Autenticacion via App Password de Google (SPEC v1.3 §3, §5.1, §7).
**Sincrono** a proposito, igual que ``classifier.py``: el worker del Paso 8
lo invocara dentro de ``asyncio.to_thread(...)`` para no bloquear el event
loop. No usamos ``aioimaplib`` / ``aiosmtplib`` aqui — la simplicidad sync
facilita los tests con factories doblados.

Marca de procesado: keyword IMAP custom ``TLY_PROCESSED``. IMAP estandar
(``STORE +FLAGS``); no usa la extension ``X-GM-LABELS`` de Gmail. Asi el
codigo es portable a cualquier proveedor IMAP si algun dia migramos.

Body parsing:

- Si el mensaje no es multipart, se usa su content-type tal cual.
- Si es multipart, se prefiere ``text/plain``; si solo hay ``text/html``,
  se convierte a texto plano via BeautifulSoup.
- ``IncomingEmail.body_source`` registra cual fuente se uso
  (``"text/plain"`` / ``"html_converted"`` / ``"empty"``). Util para
  diagnosticar clasificaciones que salgan raras: a veces la conversion
  HTML pierde contexto y eso explica la mala categorizacion.

Adjuntos: solo metadatos (nombre + tamanyo en bytes). No descargamos el
contenido (decision MVP, ver SPEC v1.3).
"""

from __future__ import annotations

import email
import email.message
import email.policy
import email.utils
import imaplib
import logging
import smtplib
import ssl
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.header import decode_header, make_header
from typing import Final, Literal

from bs4 import BeautifulSoup

from app.config import Settings, get_settings


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

#: Keyword IMAP que marca un mensaje como ya convertido en ticket.
PROCESSED_KEYWORD: Final[str] = "TLY_PROCESSED"

#: Timeout (segundos) para conexiones IMAP/SMTP. 30s da margen para picos
#: de latencia sin colgar al worker indefinidamente.
DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0


BodySource = Literal["text/plain", "html_converted", "empty"]


# ---------------------------------------------------------------------------
# Tipos publicos
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttachmentMeta:
    """Metadatos de un adjunto. ``size_bytes`` es del payload decodificado."""

    name: str
    size_bytes: int


@dataclass(frozen=True)
class IncomingEmail:
    """Mensaje parseado del INBOX, listo para que el worker lo convierta en ticket."""

    uid: str
    rfc822_message_id: str | None
    from_name: str | None
    from_email: str | None
    subject: str
    body: str
    body_source: BodySource
    received_at: datetime
    attachments: list[AttachmentMeta] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Excepciones
# ---------------------------------------------------------------------------


class EmailClientError(RuntimeError):
    """Base para errores del cliente."""


class EmailFetchError(EmailClientError):
    """FETCH IMAP fallido."""


class EmailStoreError(EmailClientError):
    """STORE IMAP fallido."""


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------

ImapFactory = Callable[..., imaplib.IMAP4_SSL]
SmtpFactory = Callable[..., smtplib.SMTP]


logger = logging.getLogger(__name__)


class EmailClient:
    """Cliente IMAP/SMTP. Las conexiones son por-operacion (abren-cierran).

    Para volumenes pequenyos (<200 tickets/mes) abrir y cerrar en cada
    operacion es perfectamente aceptable y simplifica el manejo de errores.
    Si en el futuro el volumen sube, podemos pasar a una conexion IMAP
    persistente con keepalive.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        imap_factory: ImapFactory = imaplib.IMAP4_SSL,
        smtp_factory: SmtpFactory = smtplib.SMTP,
    ) -> None:
        self._settings = settings if settings is not None else get_settings()
        self._imap_factory = imap_factory
        self._smtp_factory = smtp_factory

    # ------------------------------------------------------------------ IMAP

    @contextmanager
    def _imap_session(self) -> Iterator[imaplib.IMAP4_SSL]:
        """Abre, autentica y selecciona INBOX. Cierra al salir, sin propagar errores en cleanup."""
        s = self._settings
        conn = self._imap_factory(
            s.EMAIL_IMAP_HOST, s.EMAIL_IMAP_PORT, timeout=DEFAULT_TIMEOUT_SECONDS
        )
        try:
            conn.login(s.EMAIL_ADDRESS, s.EMAIL_APP_PASSWORD)
            conn.select("INBOX")
            yield conn
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass

    def list_unprocessed(self, *, max_results: int = 20) -> list[str]:
        """Devuelve UIDs del INBOX sin el keyword ``TLY_PROCESSED``."""
        with self._imap_session() as conn:
            typ, data = conn.uid(
                "SEARCH", None, "UNDELETED", "NOT", "KEYWORD", PROCESSED_KEYWORD
            )
            if typ != "OK":
                logger.warning(
                    "email_client list_unprocessed: SEARCH fallo typ=%s", typ
                )
                return []
            payload = data[0] if data else b""
            if not payload:
                return []
            uids = payload.split()
            return [u.decode("ascii") for u in uids[:max_results]]

    def get(self, uid: str) -> IncomingEmail:
        """Carga el mensaje completo y lo parsea."""
        with self._imap_session() as conn:
            typ, data = conn.uid("FETCH", uid, "(RFC822)")
            if typ != "OK":
                raise EmailFetchError(f"FETCH UID={uid} fallo (typ={typ})")
            raw = _extract_rfc822(data)
            if raw is None:
                raise EmailFetchError(f"FETCH UID={uid}: respuesta vacia")
            return _parse_message(uid, raw)

    def mark_processed(self, uid: str) -> None:
        """Aplica el keyword ``TLY_PROCESSED`` al mensaje. Idempotente."""
        with self._imap_session() as conn:
            typ, _ = conn.uid("STORE", uid, "+FLAGS", PROCESSED_KEYWORD)
            if typ != "OK":
                raise EmailStoreError(f"STORE UID={uid} fallo (typ={typ})")

    # ------------------------------------------------------------------ SMTP

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        in_reply_to: str | None = None,
    ) -> str:
        """Envia un mensaje text/plain UTF-8. Devuelve el ``Message-Id`` generado.

        Si ``in_reply_to`` se pasa, se anyade como header ``In-Reply-To`` y
        ``References`` (normalizado a ``<...>`` si no lo estaba). Ese
        ``rfc822_message_id`` debe venir del mensaje al que estamos
        respondiendo (campo ``IncomingEmail.rfc822_message_id``).
        """
        msg = self._build_outgoing(
            to=to, subject=subject, body=body, in_reply_to=in_reply_to
        )
        s = self._settings
        with self._smtp_factory(
            s.EMAIL_SMTP_HOST, s.EMAIL_SMTP_PORT, timeout=DEFAULT_TIMEOUT_SECONDS
        ) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(s.EMAIL_ADDRESS, s.EMAIL_APP_PASSWORD)
            smtp.send_message(msg)
        message_id = msg["Message-Id"]
        logger.info(
            "email_client send to=%s message_id=%s in_reply_to=%s",
            to,
            message_id,
            in_reply_to or "-",
        )
        return message_id

    def _build_outgoing(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        in_reply_to: str | None,
    ) -> email.message.EmailMessage:
        msg = email.message.EmailMessage()
        msg["From"] = self._settings.EMAIL_ADDRESS
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = email.utils.formatdate(localtime=False)
        domain = (
            self._settings.EMAIL_ADDRESS.rsplit("@", 1)[1]
            if "@" in self._settings.EMAIL_ADDRESS
            else "localhost"
        )
        msg["Message-Id"] = email.utils.make_msgid(domain=domain)
        if in_reply_to:
            normalized = (
                in_reply_to if in_reply_to.startswith("<") else f"<{in_reply_to}>"
            )
            msg["In-Reply-To"] = normalized
            msg["References"] = normalized
        msg.set_content(body)
        return msg


# ---------------------------------------------------------------------------
# Helpers de parseo (puros)
# ---------------------------------------------------------------------------


def _extract_rfc822(data: object) -> bytes | None:
    """Extrae el blob RFC822 de la respuesta FETCH de imaplib.

    El formato es una lista heterogenea: cada item puede ser ``bytes`` (con
    metadatos como ``"1 (UID 12 RFC822 {1234}"``), una tupla
    ``(metadata_bytes, body_bytes)``, o ``None``. Buscamos el primer body.
    """
    if not isinstance(data, list):
        return None
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])
    return None


def _parse_message(uid: str, raw: bytes) -> IncomingEmail:
    msg = email.message_from_bytes(raw)

    subject = _decode_header_value(msg.get("Subject")) or ""
    from_name, from_email_addr = _parse_from(msg.get("From"))
    rfc822_message_id = (msg.get("Message-Id") or msg.get("Message-ID")) or None
    received_at = _parse_date(msg.get("Date"))
    body, body_source = _extract_body(msg)
    attachments = _extract_attachments(msg)

    logger.info(
        "email_client get uid=%s source=%s body_len=%d attachments=%d",
        uid,
        body_source,
        len(body),
        len(attachments),
    )

    return IncomingEmail(
        uid=uid,
        rfc822_message_id=rfc822_message_id,
        from_name=from_name,
        from_email=from_email_addr,
        subject=subject,
        body=body,
        body_source=body_source,
        received_at=received_at,
        attachments=attachments,
    )


def _decode_header_value(value: str | None) -> str | None:
    """Decodifica un header con encoded-words RFC 2047 (=?utf-8?b?...?=)."""
    if value is None:
        return None
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001
        return value


def _parse_from(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    decoded = _decode_header_value(value) or value
    name, addr = email.utils.parseaddr(decoded)
    return (name or None), (addr.lower() if addr else None)


def _parse_date(value: str | None) -> datetime:
    """Parsea la cabecera ``Date``. Si falla, devuelve "ahora" UTC.

    No queremos perder un mensaje por una cabecera Date mal formada; el
    pequenyo desfase de hora en ese caso es aceptable.
    """
    if not value:
        return datetime.now(timezone.utc)
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _decode_payload(part: email.message.Message) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def _extract_body(msg: email.message.Message) -> tuple[str, BodySource]:
    """Devuelve ``(body, body_source)``.

    Prioridad: text/plain > text/html (convertido) > vacio.
    """
    if not msg.is_multipart():
        ctype = msg.get_content_type()
        text = _decode_payload(msg)
        if ctype == "text/plain":
            return text.strip(), "text/plain"
        if ctype == "text/html":
            return _html_to_text(text), "html_converted"
        return "", "empty"

    plain: str | None = None
    html: str | None = None
    for part in msg.walk():
        if part.is_multipart():
            continue
        cdisp = (part.get("Content-Disposition") or "").lower()
        if "attachment" in cdisp:
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and plain is None:
            plain = _decode_payload(part)
        elif ctype == "text/html" and html is None:
            html = _decode_payload(part)

    if plain and plain.strip():
        return plain.strip(), "text/plain"
    if html:
        return _html_to_text(html), "html_converted"
    return "", "empty"


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(separator="\n").strip()


def _extract_attachments(msg: email.message.Message) -> list[AttachmentMeta]:
    out: list[AttachmentMeta] = []
    if not msg.is_multipart():
        return out
    for part in msg.walk():
        if part.is_multipart():
            continue
        cdisp = (part.get("Content-Disposition") or "").lower()
        filename = part.get_filename()
        if filename and "attachment" in cdisp:
            payload = part.get_payload(decode=True) or b""
            decoded_name = _decode_header_value(filename) or filename
            out.append(AttachmentMeta(name=decoded_name, size_bytes=len(payload)))
    return out
