"""Orquestacion del volcado SQLite -> Google Sheets (Paso 9).

Espejo unidireccional. La fuente de verdad operativa es SQLite. Sheets
es solo presentacion (ver SPEC §4 v1.2 y CLAUDE.md §3.1).

Algoritmo de ``run_once``:

1. Abre la hoja (escribe HEADERS si vacia). Obtiene mapa ``id -> fila``.
2. Carga de SQLite los tickets pendientes con SQL:

   ::

       WHERE synced_to_sheets_at IS NULL
          OR last_updated_at > synced_to_sheets_at

   Esta condicion cubre dos casos:
   - tickets nuevos (``synced_to_sheets_at IS NULL``);
   - tickets modificados despues del ultimo sync (override de categoria
     desde la UI futura del Paso 10, ``client_notified_at`` recien
     escrito por el ``email_poller``, etc).

3. Para cada ticket: si su ID esta en el mapa, va a ``updates`` (sobre
   la fila conocida); si no, va a ``appends``.
4. ``client.apply_changes(...)``. Si OK, ``UPDATE`` en SQLite poniendo
   ``synced_to_sheets_at = now()`` para los IDs procesados — **sin
   tocar ``last_updated_at``**, sino el siguiente ciclo los volveria a
   considerar pendientes (bucle infinito).
5. Si ``apply_changes`` lanza, log WARNING y salida limpia. La BD
   queda intacta; el proximo ciclo reintenta.

**Idempotencia**: la fuente de verdad del estado de sync es
``synced_to_sheets_at`` en BD. Si Sheets queda inconsistente (por
ejemplo, alguien borra una fila a mano), basta con poner NULL a esa
fila en BD y el proximo ciclo la recrea.

**Backfill**: trivial. Tras un arranque limpio o tras vaciar la hoja,
todos los tickets tienen ``synced_to_sheets_at IS NULL`` (o sus
``last_updated_at`` son posteriores) y se subiran en el primer ciclo.
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
from app.models.ticket import from_db_row
from app.services.sheets_client import SheetsClient, SheetsClientError
from app.services.sheets_formatter import format_ticket_row


logger = logging.getLogger(__name__)


#: Tope superior de tickets procesados por iteracion. Para 200/mes
#: cualquier valor por encima de 100 es de sobra. Limita la duracion de
#: una iteracion en caso de backfill enorme (p.ej. reset masivo).
DEFAULT_MAX_PER_RUN: Final[int] = 500


ConnectFn = Callable[[str], AbstractContextManager[sqlite3.Connection]]
NowFn = Callable[[], datetime]


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(dt: datetime) -> str:
    """Serializa a ISO-8601 UTC (mismo formato que ``models.ticket._iso_utc``)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


class SheetsSync:
    """Orquestador del sync. Una instancia por proceso.

    Inyectables:

    - ``client``: ``SheetsClient`` (real o doblado en tests).
    - ``settings``: ``Settings`` (default = singleton ``get_settings()``).
    - ``connect_fn``: factory de conexion SQLite (default = ``get_connection``).
    - ``now_fn``: reloj. Util para tests deterministas.
    """

    def __init__(
        self,
        *,
        client: SheetsClient,
        settings: Settings | None = None,
        connect_fn: ConnectFn = get_connection,
        now_fn: NowFn = _default_now,
    ) -> None:
        self._client = client
        self._settings = settings if settings is not None else get_settings()
        self._connect_fn = connect_fn
        self._now_fn = now_fn

    # -------------------------------------------------------------- API

    def run_once(self, *, max_results: int = DEFAULT_MAX_PER_RUN) -> int:
        """Ejecuta un ciclo. Devuelve el numero de tickets subidos con exito.

        Nunca lanza al llamante: cualquier excepcion se convierte en log y
        ``0``. El scheduler vuelve a invocar en el siguiente intervalo.
        """
        try:
            return self._run_once_inner(max_results=max_results)
        except SheetsClientError as exc:
            logger.warning(
                "sheets_sync API fallo: %s. Proximo ciclo reintentara.",
                exc,
            )
            return 0
        except Exception as exc:  # noqa: BLE001 — defensa: nada tira al scheduler
            logger.exception(
                "sheets_sync excepcion inesperada reason=%s",
                type(exc).__name__,
            )
            return 0

    # ----------------------------------------------------------- privado

    def _run_once_inner(self, *, max_results: int) -> int:
        ws, id_to_row = self._client.read_state()

        with self._connect_fn(self._settings.SQLITE_PATH) as conn:
            tickets = self._load_pending(conn, limit=max_results)
            if not tickets:
                return 0

            updates: list[tuple[int, list[str]]] = []
            appends: list[list[str]] = []
            ids_processed: list[str] = []

            for ticket in tickets:
                row = format_ticket_row(ticket)
                if ticket.id in id_to_row:
                    updates.append((id_to_row[ticket.id], row))
                else:
                    appends.append(row)
                ids_processed.append(ticket.id)

            # Llama al API. Si lanza, el ``except`` de ``run_once`` la
            # captura y NO marcamos nada como sincronizado.
            self._client.apply_changes(ws, updates=updates, appends=appends)

            # Solo aqui marcamos: si llegamos hasta este punto, la
            # llamada de red fue OK.
            self._mark_synced(conn, ids_processed)

        logger.info(
            "sheets_sync exito subidos=%d (updates=%d, appends=%d)",
            len(ids_processed),
            len(updates),
            len(appends),
        )
        return len(ids_processed)

    def _load_pending(
        self, conn: sqlite3.Connection, *, limit: int
    ) -> list:
        """Carga tickets con sync pendiente, ordenados por ``created_at``.

        Orden: ASC por ``created_at`` para que en backfill las filas
        aparezcan en orden cronologico ascendente (mas legible al
        revisar la hoja).
        """
        rows = conn.execute(
            "SELECT * FROM tickets "
            "WHERE synced_to_sheets_at IS NULL "
            "   OR last_updated_at > synced_to_sheets_at "
            "ORDER BY created_at ASC, id ASC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
        return [from_db_row(r) for r in rows]

    def _mark_synced(
        self, conn: sqlite3.Connection, ticket_ids: list[str]
    ) -> None:
        """``UPDATE`` masivo para los IDs procesados.

        Importante: NO toca ``last_updated_at``. Si lo tocara, el siguiente
        ciclo veria estos tickets como modificados y volveria a
        sincronizarlos en bucle.
        """
        if not ticket_ids:
            return
        now_iso = _iso_utc(self._now_fn())
        placeholders = ",".join(["?"] * len(ticket_ids))
        conn.execute(
            f"UPDATE tickets SET synced_to_sheets_at = ? WHERE id IN ({placeholders})",
            [now_iso, *ticket_ids],
        )
