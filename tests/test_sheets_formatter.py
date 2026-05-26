"""Tests del formateo de tickets para Google Sheets.

Sin BD, sin red. Solo funciones puras.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models.ticket import (
    Attachment,
    Ticket,
    TicketCategory,
    TicketChannel,
    TicketStatus,
)
from app.services.sheets_formatter import (
    HEADERS,
    format_attachments,
    format_datetime_madrid,
    format_ticket_row,
)


# ---------------------------------------------------------------------------
# Cabeceras y orden
# ---------------------------------------------------------------------------


def test_headers_tienen_exactamente_17_columnas():
    assert len(HEADERS) == 17


def test_headers_orden_y_nombres_exactos():
    # Si esto falla, alguien reordeno o renombro columnas. Hay que decidir
    # explicitamente y reescribir las cabeceras de la hoja real.
    assert HEADERS == (
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


def test_headers_no_incluye_raw_message_id_ni_reasoning():
    assert "raw_message_id" not in HEADERS
    assert "category_reasoning" not in HEADERS
    assert "Razonamiento" not in HEADERS


# ---------------------------------------------------------------------------
# format_datetime_madrid: DST y nulls
# ---------------------------------------------------------------------------


def test_formatea_datetime_verano_cest_es_utc_mas_2():
    # 14 mayo 2026 09:23:11 UTC -> 11:23:11 Madrid (CEST, +02:00)
    dt = datetime(2026, 5, 14, 9, 23, 11, tzinfo=timezone.utc)
    assert format_datetime_madrid(dt) == "2026-05-14 11:23:11"


def test_formatea_datetime_invierno_cet_es_utc_mas_1():
    # 15 enero 2026 09:23:11 UTC -> 10:23:11 Madrid (CET, +01:00)
    dt = datetime(2026, 1, 15, 9, 23, 11, tzinfo=timezone.utc)
    assert format_datetime_madrid(dt) == "2026-01-15 10:23:11"


def test_formatea_datetime_transicion_dst_marzo():
    # DST 2026: empieza el domingo 29 marzo a las 02:00 Madrid -> +1h.
    # 29 marzo 2026 00:30 UTC = 01:30 CET (todavia invierno).
    # 29 marzo 2026 01:30 UTC = 03:30 CEST (despues del salto).
    before = datetime(2026, 3, 29, 0, 30, 0, tzinfo=timezone.utc)
    after = datetime(2026, 3, 29, 1, 30, 0, tzinfo=timezone.utc)
    assert format_datetime_madrid(before) == "2026-03-29 01:30:00"
    assert format_datetime_madrid(after) == "2026-03-29 03:30:00"


def test_formatea_datetime_none_devuelve_cadena_vacia():
    assert format_datetime_madrid(None) == ""


def test_formatea_datetime_naive_asume_utc():
    # Naive -> tratado como UTC. 14 mayo 12:00 naive -> 14:00 Madrid (CEST).
    dt = datetime(2026, 5, 14, 12, 0, 0)
    assert format_datetime_madrid(dt) == "2026-05-14 14:00:00"


# ---------------------------------------------------------------------------
# format_attachments
# ---------------------------------------------------------------------------


def test_formatea_adjuntos_lista_vacia_devuelve_cadena_vacia():
    assert format_attachments([]) == ""


def test_formatea_adjuntos_un_archivo_con_tamano_kb_redondeado():
    # 250 * 1024 = 256000 -> redondea a 250 KB exactos.
    att = Attachment(name="factura.pdf", size_bytes=256_000)
    assert format_attachments([att]) == "factura.pdf (250 KB)"


def test_formatea_adjuntos_redondeo_explicito():
    # 1500 bytes -> 1500/1024 = 1.46... -> redondea a 1 KB.
    # 1800 bytes -> 1800/1024 = 1.76... -> redondea a 2 KB.
    assert format_attachments([Attachment(name="a.txt", size_bytes=1500)]) == "a.txt (1 KB)"
    assert format_attachments([Attachment(name="b.txt", size_bytes=1800)]) == "b.txt (2 KB)"


def test_formatea_adjuntos_varios_separados_por_punto_y_coma():
    atts = [
        Attachment(name="factura.pdf", size_bytes=256_000),
        Attachment(name="foto.jpg", size_bytes=46_080),  # 45 KB exactos
    ]
    assert format_attachments(atts) == "factura.pdf (250 KB); foto.jpg (45 KB)"


def test_formatea_adjuntos_size_none_devuelve_guion():
    att = Attachment(name="raro.bin", size_bytes=None)
    assert format_attachments([att]) == "raro.bin (-)"


def test_formatea_adjuntos_mezcla_size_y_none():
    atts = [
        Attachment(name="ok.pdf", size_bytes=1024),
        Attachment(name="raro.bin", size_bytes=None),
    ]
    assert format_attachments(atts) == "ok.pdf (1 KB); raro.bin (-)"


# ---------------------------------------------------------------------------
# format_ticket_row: caso completo y casos limite
# ---------------------------------------------------------------------------


def _make_ticket(**overrides) -> Ticket:
    """Helper: ticket con valores por defecto razonables."""
    defaults = dict(
        id="TLY-2026-0042",
        created_at=datetime(2026, 5, 14, 9, 23, 11, tzinfo=timezone.utc),
        channel=TicketChannel.EMAIL,
        from_name="Maria Gonzalez",
        from_email="maria@example.com",
        from_phone=None,
        subject="Pedido 12345 no llegado",
        body="Buenos dias, hice el pedido el lunes...",
        status=TicketStatus.NEW,
        category=TicketCategory.COMERCIAL,
        category_confidence=0.92,
        category_reasoning="contiene la palabra pedido",
        category_manual_override=False,
        needs_review=False,
        raw_message_id="<abc@example.com>",
        attachments=[Attachment(name="factura.pdf", size_bytes=256_000)],
        client_notified_at=datetime(2026, 5, 14, 9, 23, 45, tzinfo=timezone.utc),
        last_updated_at=datetime(2026, 5, 14, 9, 23, 45, tzinfo=timezone.utc),
        synced_to_sheets_at=datetime(2026, 5, 14, 9, 24, 2, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return Ticket(**defaults)


def test_fila_completa_17_columnas():
    ticket = _make_ticket()
    row = format_ticket_row(ticket)
    assert len(row) == 17


def test_fila_completa_valores_caso_realista():
    ticket = _make_ticket()
    row = format_ticket_row(ticket)
    assert row == [
        "TLY-2026-0042",
        "2026-05-14 11:23:11",
        "email",
        "Maria Gonzalez",
        "maria@example.com",
        "",
        "Pedido 12345 no llegado",
        "Buenos dias, hice el pedido el lunes...",
        "NEW",
        "COMERCIAL",
        "0.92",
        "FALSE",
        "FALSE",
        "factura.pdf (250 KB)",
        "2026-05-14 11:23:45",
        "2026-05-14 11:23:45",
        "2026-05-14 11:24:02",
    ]


def test_fila_categoria_null_se_serializa_como_cadena_vacia():
    ticket = _make_ticket(
        category=None,
        category_confidence=None,
        category_reasoning=None,
        needs_review=True,
    )
    row = format_ticket_row(ticket)
    # Indice 9 = Categoria, 10 = Confianza
    assert row[9] == ""
    assert row[10] == ""
    # Revision pendiente refleja needs_review=True
    assert row[12] == "TRUE"


def test_fila_remitente_anonimo_no_revienta():
    ticket = _make_ticket(from_name=None, from_email=None)
    row = format_ticket_row(ticket)
    assert row[3] == ""  # Nombre remitente
    assert row[4] == ""  # Email remitente


def test_fila_sin_adjuntos_devuelve_cadena_vacia_en_columna_adjuntos():
    ticket = _make_ticket(attachments=[])
    row = format_ticket_row(ticket)
    # Indice 13 = Adjuntos.
    assert row[13] == ""


def test_fila_categoria_manual_true():
    ticket = _make_ticket(category_manual_override=True)
    row = format_ticket_row(ticket)
    assert row[11] == "TRUE"


def test_fila_synced_at_null_se_serializa_como_cadena_vacia():
    ticket = _make_ticket(synced_to_sheets_at=None)
    row = format_ticket_row(ticket)
    # Indice 16 = Sincronizado
    assert row[16] == ""


def test_fila_cuerpo_largo_no_se_trunca():
    cuerpo = "a" * 10_000
    ticket = _make_ticket(body=cuerpo)
    row = format_ticket_row(ticket)
    # Indice 7 = Cuerpo
    assert row[7] == cuerpo


def test_fila_channel_email():
    ticket = _make_ticket(channel=TicketChannel.EMAIL)
    assert format_ticket_row(ticket)[2] == "email"


# Sanity: cualquier fila tiene el mismo numero de elementos que HEADERS.
@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"category": None, "category_confidence": None},
        {"attachments": []},
        {"client_notified_at": None, "synced_to_sheets_at": None},
    ],
)
def test_fila_siempre_tiene_misma_longitud_que_headers(overrides):
    ticket = _make_ticket(**overrides)
    assert len(format_ticket_row(ticket)) == len(HEADERS)
