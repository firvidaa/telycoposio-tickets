"""Worker de polling de email: lee INBOX y crea tickets (Paso 8).

Disenyo y compromisos:

- **APScheduler in-process.** Para 200 tickets/mes en un solo contenedor,
  separar el worker en otro proceso seria sobreingeniería. El scheduler se
  arranca desde el lifespan de FastAPI (ver ``app/main.py``) con
  ``max_instances=1`` y ``coalesce=True``: si una iteracion tarda mas que
  el intervalo, la siguiente no solapa, se descarta.

- **Idempotencia por capas.** Si la app se cae a medio ciclo, el reintento
  no duplica nada:

  1. *Ticket en BD:* ``ticket_service.create_ticket`` deduplica por
     ``raw_message_id`` (UNIQUE). Reintento -> devuelve el existente sin
     crear duplicado.
  2. *Confirmacion al cliente:* ``tickets.client_notified_at`` actua como
     llave de idempotencia. Solo enviamos confirmacion si esta ``NULL``;
     si ya tenia valor (reintento puro de IMAP), saltamos.
  3. *Notificacion interna:* best-effort, **no** trackeada. Decision
     deliberada (ver SPEC y conversacion del Paso 8): un duplicado interno
     ocasional es ruido aceptable; trackearlo seria sobreingeniería.
     Como solo se envia cuando se envia la confirmacion al cliente, los
     reintentos puros de ``mark_processed`` no la reenvian.
  4. *Marca IMAP:* ``client.mark_processed`` saca el UID del bucle.

  **Ventana conocida.** Entre ``SMTP-OK de la confirmacion`` y
  ``UPDATE client_notified_at`` hay milisegundos. Si la app cae justo ahi,
  el reintento reenviara la confirmacion al cliente. Tradeoff asumido
  frente a un 2-phase commit, que seria sobreingeniería para el volumen.

- **Bucle infinito de emails problematicos.** Contador en memoria por UID
  (vida del proceso). Si un mismo UID falla ``MAX_FAILURES_PER_UID``
  veces consecutivas, lo marcamos procesado y logueamos ``ERROR``. Mejor
  "perder" un email roto (queda en el INBOX para revision humana) que
  atascar el sistema con ruido infinito. Si ``mark_processed`` tambien
  falla, el contador en memoria lo mantiene fuera del bucle hasta el
  proximo reinicio del proceso.

- **Concurrencia con el event loop.** ``run_once`` es sincrono. El
  scheduler lo ejecuta dentro de ``asyncio.to_thread(...)`` (ver
  ``app/main.py``) para no bloquear el event loop. ``max_instances=1``
  garantiza que nunca hay dos ``run_once`` simultaneos.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from typing import Final

from app.config import Settings, get_settings
from app.db.sqlite import get_connection
from app.models.ticket import Attachment, TicketChannel
from app.services.classifier import ClassificationResult, classify
from app.services.email_client import (
    EmailClient,
    EmailClientError,
    IncomingEmail,
)
from app.services.ticket_service import create_ticket


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

#: Maximo de fallos consecutivos por UID antes de declararlo "toxico" y
#: sacarlo del bucle. 5 ciclos x 60s = 5 minutos: suficiente para absorber
#: blips transitorios de IMAP/SMTP/Anthropic; mas alla casi siempre es un
#: fallo determinista (encoding raro, body inesperado, etc.).
MAX_FAILURES_PER_UID: Final[int] = 5

#: Tope superior de UIDs procesados por iteracion. Mas que generoso para
#: 200 tickets/mes (~7/dia). Limita la duracion de cada iteracion a algo
#: predecible aunque haya un atasco puntual.
DEFAULT_MAX_PER_RUN: Final[int] = 20

#: Truncado del subject del cliente para la notificacion interna. 80 chars
#: caben en una linea con el prefijo ``[Ticket TLY-2026-XXXX]``.
INTERNAL_SUBJECT_MAX: Final[int] = 80

#: Maximo de chars de vista previa del body en la notificacion interna.
INTERNAL_BODY_PREVIEW_CHARS: Final[int] = 500


# ---------------------------------------------------------------------------
# Plantillas (constantes module-level, aprobadas en Paso 8)
# ---------------------------------------------------------------------------

CONFIRMATION_SUBJECT_TEMPLATE: Final[str] = "Re: {original_subject} [#{ticket_id}]"


CONFIRMATION_BODY_TEMPLATE: Final[str] = """\
Hola{greeting_suffix},

Hemos recibido tu solicitud. Tu numero de referencia es
#{ticket_id}; guardalo por si necesitas hacer seguimiento.

Un saludo,
Equipo Telycoposio

---
Aviso de privacidad: tus datos seran tratados por Telycoposio
para gestionar tu solicitud. Si deseas ejercer tus derechos de
acceso, rectificacion o supresion, contactanos respondiendo a
este correo.
"""


INTERNAL_NOTIFICATION_SUBJECT_TEMPLATE: Final[str] = (
    "[Ticket {ticket_id}] {subject_truncated}"
)


INTERNAL_NOTIFICATION_BODY_TEMPLATE: Final[str] = """\
Nuevo ticket: #{ticket_id}
Categoria    : {category} (confianza {confidence_pct})
Razonamiento : {reasoning}
Remitente    : {from_display}
Asunto       : {original_subject}
Ver ticket   : {ticket_url}

---- Vista previa (primeros 500 chars) ----
{body_preview}
"""


# ---------------------------------------------------------------------------
# Tipos
# ---------------------------------------------------------------------------

#: Callable que clasifica un email a partir de (subject, body). Inyectable
#: para tests; el default invoca ``classifier.classify`` (que nunca lanza).
ClassifyFn = Callable[[str, str], ClassificationResult]

#: Callable que recibe el path SQLite y devuelve un context manager de
#: ``sqlite3.Connection``. Default = ``app.db.sqlite.get_connection``.
ConnectFn = Callable[[str], AbstractContextManager[sqlite3.Connection]]

#: Callable sin argumentos que devuelve "ahora" en UTC. Inyectable para
#: tests deterministas.
NowFn = Callable[[], datetime]


# ---------------------------------------------------------------------------
# Helpers de plantillas (puros)
# ---------------------------------------------------------------------------


def _build_greeting_suffix(from_name: str | None) -> str:
    """Construye el sufijo del saludo: ``" Juan Perez"`` o cadena vacia."""
    if from_name and from_name.strip():
        return " " + from_name.strip()
    return ""


def _build_confirmation_subject(*, original_subject: str, ticket_id: str) -> str:
    return CONFIRMATION_SUBJECT_TEMPLATE.format(
        original_subject=original_subject, ticket_id=ticket_id
    )


def _build_confirmation_body(*, ticket_id: str, from_name: str | None) -> str:
    return CONFIRMATION_BODY_TEMPLATE.format(
        greeting_suffix=_build_greeting_suffix(from_name),
        ticket_id=ticket_id,
    )


def _build_internal_subject(*, ticket_id: str, original_subject: str) -> str:
    return INTERNAL_NOTIFICATION_SUBJECT_TEMPLATE.format(
        ticket_id=ticket_id,
        subject_truncated=original_subject[:INTERNAL_SUBJECT_MAX],
    )


def _build_from_display(from_name: str | None, from_email: str | None) -> str:
    if from_name and from_email:
        return f"{from_name} <{from_email}>"
    if from_email:
        return from_email
    if from_name:
        return from_name
    return "(desconocido)"


def _build_internal_body(
    *,
    ticket_id: str,
    result: ClassificationResult,
    email_msg: IncomingEmail,
    base_url: str,
) -> str:
    if result.category is None:
        category_str = "(sin clasificar - needs_review)"
    else:
        category_str = result.category.value
    if result.confidence is None:
        confidence_pct = "n/a"
    else:
        confidence_pct = f"{int(round(result.confidence * 100))}%"
    reasoning_str = result.reasoning or "(la IA no devolvio razonamiento)"
    return INTERNAL_NOTIFICATION_BODY_TEMPLATE.format(
        ticket_id=ticket_id,
        category=category_str,
        confidence_pct=confidence_pct,
        reasoning=reasoning_str,
        from_display=_build_from_display(email_msg.from_name, email_msg.from_email),
        original_subject=email_msg.subject,
        ticket_url=f"{base_url}/tickets/{ticket_id}",
        body_preview=email_msg.body[:INTERNAL_BODY_PREVIEW_CHARS],
    )


def _iso_utc(dt: datetime) -> str:
    """Serializa un datetime a ISO-8601 UTC. Coincide con ``models.ticket._iso_utc``."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Defaults inyectables
# ---------------------------------------------------------------------------


def _default_classify(subject: str, body: str) -> ClassificationResult:
    """Adaptador a la firma kwargs-only de ``classifier.classify``."""
    return classify(subject=subject, body=body)


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# EmailPoller
# ---------------------------------------------------------------------------


class EmailPoller:
    """Procesa el INBOX y crea tickets. Una instancia por proceso.

    ``run_once`` es sincrono. El scheduler de ``app/main.py`` lo invoca via
    ``asyncio.to_thread(...)`` para no bloquear el event loop. Con
    ``max_instances=1`` jamas hay dos ``run_once`` en paralelo.

    Inyectables (los defaults tiran de los modulos reales):

    - ``email_client`` (obligatorio): cliente IMAP/SMTP. Tests pasan un
      double con la misma superficie.
    - ``classify_fn``: clasificador (subject, body) -> ClassificationResult.
      Nunca debe lanzar (el default ``classifier.classify`` lo garantiza).
    - ``connect_fn``: factory de conexion SQLite.
    - ``now_fn``: reloj. Util para tests deterministas.
    """

    def __init__(
        self,
        *,
        email_client: EmailClient,
        settings: Settings | None = None,
        classify_fn: ClassifyFn = _default_classify,
        connect_fn: ConnectFn = get_connection,
        now_fn: NowFn = _default_now,
    ) -> None:
        self._client = email_client
        self._settings = settings if settings is not None else get_settings()
        self._classify_fn = classify_fn
        self._connect_fn = connect_fn
        self._now_fn = now_fn
        # Contador de fallos consecutivos por UID. Vive en memoria del
        # proceso; si reiniciamos, se resetea (un email "toxico" tendra
        # otras MAX_FAILURES oportunidades, lo cual es aceptable).
        self._failure_counts: dict[str, int] = {}

    # ----------------------------------------------------------------- API

    def run_once(self, *, max_results: int = DEFAULT_MAX_PER_RUN) -> int:
        """Procesa hasta ``max_results`` UIDs no procesados.

        Devuelve el numero de UIDs procesados con exito. UIDs ya marcados
        como toxicos en memoria se filtran y no se reintentan.
        """
        try:
            uids = self._client.list_unprocessed(max_results=max_results)
        except EmailClientError as exc:
            # No subimos: el scheduler lo invocara otra vez. Loguear y salir.
            logger.warning(
                "poller list_unprocessed fallo reason=%s",
                type(exc).__name__,
            )
            return 0
        except Exception as exc:  # noqa: BLE001 — defensa: nada tira al scheduler
            logger.exception(
                "poller list_unprocessed excepcion inesperada reason=%s",
                type(exc).__name__,
            )
            return 0

        if not uids:
            return 0

        # Filtra UIDs ya declarados toxicos en memoria. Se quedan fuera
        # del bucle aunque `mark_processed` haya fallado al sacarlos.
        live_uids = [
            u for u in uids if self._failure_counts.get(u, 0) < MAX_FAILURES_PER_UID
        ]

        processed = 0
        for uid in live_uids:
            try:
                ok = self._process_one(uid)
            except Exception as exc:  # noqa: BLE001
                # Defensa en profundidad: si _process_one se sale por una via
                # imprevista, el resto de la cola sigue procesandose.
                logger.exception(
                    "poller _process_one excepcion inesperada uid=%s reason=%s",
                    uid,
                    type(exc).__name__,
                )
                ok = False

            if ok:
                # Exito -> limpiar contador para no acumular memoria de UIDs
                # ya procesados (aunque ya no saldran en list_unprocessed).
                self._failure_counts.pop(uid, None)
                processed += 1
            else:
                count = self._failure_counts.get(uid, 0) + 1
                self._failure_counts[uid] = count
                if count >= MAX_FAILURES_PER_UID:
                    self._declare_toxic(uid)

        return processed

    # -------------------------------------------------------------- helpers

    def _declare_toxic(self, uid: str) -> None:
        """Saca un UID del bucle tras ``MAX_FAILURES_PER_UID`` fallos.

        Loguea ``ERROR`` ruidoso (es la senyal humana para investigar) y
        llama a ``mark_processed`` para que no vuelva a aparecer en
        ``list_unprocessed``. Si ``mark_processed`` tambien falla, el
        contador en memoria mantiene el UID fuera del bucle hasta el
        siguiente reinicio del proceso.
        """
        logger.error(
            "POLLER UID toxico uid=%s: %d fallos consecutivos. "
            "Lo marco como procesado para no atascar el sistema. "
            "Revisar manualmente el mensaje en el INBOX de Gmail.",
            uid,
            MAX_FAILURES_PER_UID,
        )
        try:
            self._client.mark_processed(uid)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "POLLER no se pudo marcar UID toxico uid=%s reason=%s; "
                "el contador en memoria lo bloqueara hasta reinicio.",
                uid,
                type(exc).__name__,
            )

    def _process_one(self, uid: str) -> bool:
        """Procesa un UID completo. ``True`` solo si todos los pasos exitosos.

        Pasos:

        1. FETCH del mensaje (``email_client.get``).
        2. Clasificar (no lanza nunca).
        3. ``create_ticket`` (idempotente por ``raw_message_id``).
        4. Si ``client_notified_at IS NULL``: enviar confirmacion al
           cliente, hacer ``UPDATE``, enviar notificacion interna.
        5. ``mark_processed``.

        Cualquier paso fallido devuelve ``False`` y el llamante incrementa
        el contador de UID. La excepcion **no** se propaga.
        """
        try:
            email_msg = self._client.get(uid)
        except EmailClientError as exc:
            logger.warning(
                "poller fetch fallo uid=%s reason=%s", uid, type(exc).__name__
            )
            return False

        # Clasificador: garantiza no-lanzar. Si falla, result vacio y el
        # ticket queda con ``needs_review=True`` (regla del SPEC §4.1).
        result = self._classify_fn(email_msg.subject, email_msg.body)

        with self._connect_fn(self._settings.SQLITE_PATH) as conn:
            try:
                ticket = create_ticket(
                    conn=conn,
                    channel=TicketChannel.EMAIL,
                    subject=email_msg.subject,
                    body=email_msg.body,
                    from_name=email_msg.from_name,
                    from_email=email_msg.from_email,
                    raw_message_id=email_msg.rfc822_message_id,
                    attachments=[
                        Attachment(name=a.name, size_bytes=a.size_bytes)
                        for a in email_msg.attachments
                    ],
                    category=result.category,
                    category_confidence=result.confidence,
                    category_reasoning=result.reasoning,
                    now=self._now_fn(),
                    prefix=self._settings.TICKET_PREFIX,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "poller create_ticket fallo uid=%s reason=%s",
                    uid,
                    type(exc).__name__,
                )
                return False

            # Idempotencia de la confirmacion: si el ticket ya tenia
            # client_notified_at, este es un reintento puro post-SMTP y no
            # debemos reenviar nada (ni confirmacion ni notif interna).
            needs_confirmation = ticket.client_notified_at is None

            if needs_confirmation:
                if email_msg.from_email is None:
                    # Sin direccion de retorno no podemos confirmar. Lo
                    # logueamos, dejamos client_notified_at en NULL y
                    # marcamos procesado (no entra al bucle de fallos).
                    logger.warning(
                        "poller uid=%s ticket=%s sin from_email: "
                        "ticket creado sin confirmacion.",
                        uid,
                        ticket.id,
                    )
                else:
                    sent_ok = self._send_confirmation(uid, ticket.id, email_msg)
                    if not sent_ok:
                        return False

                    now_iso = _iso_utc(self._now_fn())
                    conn.execute(
                        "UPDATE tickets SET client_notified_at = ?, "
                        "last_updated_at = ? WHERE id = ?",
                        (now_iso, now_iso, ticket.id),
                    )

                    # Notificacion interna best-effort: errores se logean
                    # y se ignoran (no impiden marcar procesado).
                    self._send_internal_notification(
                        ticket_id=ticket.id, email_msg=email_msg, result=result
                    )

        # mark_processed FUERA de la transaccion: si la conexion fallase al
        # cerrar, la marca IMAP no tiene nada que ver con la BD.
        try:
            self._client.mark_processed(uid)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "poller mark_processed fallo uid=%s reason=%s",
                uid,
                type(exc).__name__,
            )
            return False

        logger.info("poller exito uid=%s ticket=%s", uid, ticket.id)
        return True

    def _send_confirmation(
        self, uid: str, ticket_id: str, email_msg: IncomingEmail
    ) -> bool:
        """Envia la confirmacion al cliente. ``False`` si SMTP falla."""
        try:
            self._client.send(
                to=email_msg.from_email or "",  # nunca llega aqui con None
                subject=_build_confirmation_subject(
                    original_subject=email_msg.subject,
                    ticket_id=ticket_id,
                ),
                body=_build_confirmation_body(
                    ticket_id=ticket_id, from_name=email_msg.from_name
                ),
                in_reply_to=email_msg.rfc822_message_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "poller send confirmation fallo uid=%s ticket=%s reason=%s",
                uid,
                ticket_id,
                type(exc).__name__,
            )
            return False
        return True

    def _send_internal_notification(
        self,
        *,
        ticket_id: str,
        email_msg: IncomingEmail,
        result: ClassificationResult,
    ) -> None:
        """Best-effort. No lanza nunca: cualquier error se loguea y se ignora.

        Si ``INTERNAL_NOTIFICATION_EMAIL`` no esta configurado, no hace nada.
        """
        recipient = self._settings.INTERNAL_NOTIFICATION_EMAIL
        if not recipient:
            return
        try:
            self._client.send(
                to=recipient,
                subject=_build_internal_subject(
                    ticket_id=ticket_id, original_subject=email_msg.subject
                ),
                body=_build_internal_body(
                    ticket_id=ticket_id,
                    result=result,
                    email_msg=email_msg,
                    base_url=self._settings.APP_BASE_URL,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "poller internal notif fallo ticket=%s reason=%s",
                ticket_id,
                type(exc).__name__,
            )
