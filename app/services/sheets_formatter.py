"""Formateo de tickets para volcado a Google Sheets (Paso 9).

Funciones puras, sin gspread, sin BD, sin red. Son las unicas que
conocen el formato de presentacion (fechas Madrid, KB, TRUE/FALSE).

Decisiones de formato (ver discusion del Paso 9):

- **Fechas en hora Madrid** (no UTC) con ``YYYY-MM-DD HH:MM:SS``. La BD
  sigue almacenando en UTC; la conversion es solo al volcar. Manejamos
  DST automaticamente via ``zoneinfo.ZoneInfo("Europe/Madrid")``.

- **Adjuntos**: ``"nombre.ext (NNN KB); ..."``. Tamano = redondeo a KB.
  Si ``size_bytes`` es ``None`` (no deberia pasar con el cliente IMAP
  actual, pero el modelo lo permite), usamos ``"nombre.ext (-)"``.

- **Booleanos** como ``TRUE``/``FALSE`` literales (Sheets los reconoce
  como booleanos nativos: checkbox).

- **NULLs** se serializan como ``""`` (Sheets distingue celda vacia de
  ``"None"``; preferimos lo primero).

- ``raw_message_id`` y ``category_reasoning`` se **excluyen**
  deliberadamente (Subset legible aprobado en el Paso 9).
"""

from __future__ import annotations

from datetime import datetime
from typing import Final
from zoneinfo import ZoneInfo

from app.models.ticket import Attachment, Ticket


#: Cabeceras en el orden de las columnas. Cambiar este orden es un cambio
#: de esquema visible para el operador: si se hace, hay que reescribir las
#: cabeceras de la hoja (la app las recrea si esta vacia, pero no
#: reordena una hoja existente).
HEADERS: Final[tuple[str, ...]] = (
    "ID",
    "Creado",
    "Canal",
    "Nombre remitente",
    "Email remitente",
    "Telefono remitente",
    "Asunto",
    "Cuerpo",
    "Estado",
    "Categoria",
    "Confianza categoria",
    "Categoria manual",
    "Revision pendiente",
    "Adjuntos",
    "Cliente notificado",
    "Actualizado",
    "Sincronizado",
)

#: Zona horaria de Telycoposio. ``ZoneInfo`` aplica DST automaticamente
#: (CET = UTC+1 en invierno, CEST = UTC+2 en verano).
_MADRID = ZoneInfo("Europe/Madrid")


def format_datetime_madrid(value: datetime | None) -> str:
    """Convierte un datetime (aware en UTC o naive) a ``YYYY-MM-DD HH:MM:SS`` Madrid.

    - ``None`` -> cadena vacia.
    - Naive -> asumimos UTC (coherente con ``models.ticket._iso_utc``).
    - Aware -> reconvertimos a Madrid.

    No incluimos sufijo de zona ni offset: el formato pactado con el
    operador asume Madrid implicitamente. Cualquier confusion DST se
    resuelve viendo el log o el campo ``Actualizado`` de la BD en UTC.
    """
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(_MADRID).strftime("%Y-%m-%d %H:%M:%S")


def format_attachments(attachments: list[Attachment]) -> str:
    """Serializa la lista de adjuntos a ``"nombre (KB KB); ..."``.

    - Lista vacia -> ``""``.
    - ``size_bytes`` None -> ``"nombre (-)"`` (con guion como marcador
      explicito de "tamano no disponible").
    - Tamanos redondeados a KB (entero). Los emails reales pesan
      KB-MB; mostrar bytes seria ruido.
    """
    if not attachments:
        return ""
    parts: list[str] = []
    for att in attachments:
        if att.size_bytes is None:
            parts.append(f"{att.name} (-)")
        else:
            kb = round(att.size_bytes / 1024)
            parts.append(f"{att.name} ({kb} KB)")
    return "; ".join(parts)


def _bool_str(value: bool) -> str:
    """``True`` -> ``"TRUE"``; ``False`` -> ``"FALSE"``.

    Sheets convierte estos literales a casillas booleanas nativas con
    su sistema de tipos. Si pusieramos ``"True"`` / ``"si"`` quedarian
    como texto.
    """
    return "TRUE" if value else "FALSE"


def _confidence_str(value: float | None) -> str:
    if value is None:
        return ""
    # Conservamos 2 decimales: la API de Anthropic no garantiza mas
    # precision que eso y muchos decimales solo anyaden ruido visual.
    return f"{value:.2f}"


def format_ticket_row(ticket: Ticket) -> list[str]:
    """Devuelve la fila como ``list[str]`` en el mismo orden que ``HEADERS``.

    17 elementos exactamente. Si los tipos del modelo cambian, este
    formatter es el unico punto que hay que actualizar.
    """
    return [
        ticket.id,
        format_datetime_madrid(ticket.created_at),
        ticket.channel.value,
        ticket.from_name or "",
        ticket.from_email or "",
        ticket.from_phone or "",
        ticket.subject,
        ticket.body,
        ticket.status.value,
        ticket.category.value if ticket.category is not None else "",
        _confidence_str(ticket.category_confidence),
        _bool_str(ticket.category_manual_override),
        _bool_str(ticket.needs_review),
        format_attachments(ticket.attachments),
        format_datetime_madrid(ticket.client_notified_at),
        format_datetime_madrid(ticket.last_updated_at),
        format_datetime_madrid(ticket.synced_to_sheets_at),
    ]
