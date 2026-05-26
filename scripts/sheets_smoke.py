"""CLI para verificar manualmente el sync de tickets a Google Sheets.

Llama a ``SheetsSync.run_once()`` una sola vez y termina. Util para
validar credenciales, ID de spreadsheet y formato sin esperar al
scheduler (que corre cada GSHEETS_SYNC_INTERVAL_SECONDS).

Uso:

    python scripts/sheets_smoke.py
        Sube todos los tickets pendientes a la hoja configurada.

    python scripts/sheets_smoke.py --reset-sync
        Pone ``synced_to_sheets_at = NULL`` en TODOS los tickets antes de
        sincronizar. Util si has borrado filas a mano en la hoja y
        quieres recrearlas. Pide confirmacion.

    python scripts/sheets_smoke.py --max 10
        Limita el numero de tickets procesados en este run.

Logs (INFO) por stderr; resultado por stdout.

Nota: este script NO requiere ``GSHEETS_ENABLED=true`` (puentea el flag,
ya que el operador lo invoca explicitamente). Si pone el flag a true
arranca tambien el scheduler en el ``uvicorn`` siguiente.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from app.config import get_settings  # noqa: E402
from app.db.sqlite import get_connection  # noqa: E402
from app.services.sheets_client import SheetsClient  # noqa: E402
from app.services.sheets_sync import SheetsSync  # noqa: E402


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _reset_sync(db_path: str) -> int:
    """Pone ``synced_to_sheets_at = NULL`` en todos los tickets.

    Devuelve el numero de filas afectadas.
    """
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "UPDATE tickets SET synced_to_sheets_at = NULL"
        )
        return cur.rowcount or 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset-sync",
        action="store_true",
        help="Limpia synced_to_sheets_at en todos los tickets antes de sync.",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=500,
        help="Maximo de tickets a procesar en este run (default 500).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Salta la confirmacion interactiva de --reset-sync.",
    )
    args = parser.parse_args()

    _setup_logging()
    settings = get_settings()

    # No exigimos GSHEETS_ENABLED=true para el script: el operador esta
    # invocandolo a mano. Pero si las credenciales faltan, no podemos
    # hacer nada utilmente.
    if not settings.GSHEETS_SPREADSHEET_ID or not settings.GSHEETS_CREDENTIALS_PATH:
        sys.stderr.write(
            "ERROR: faltan GSHEETS_SPREADSHEET_ID o GSHEETS_CREDENTIALS_PATH "
            "en .env. No puedo continuar.\n"
        )
        return 2

    if args.reset_sync:
        if not args.yes:
            sys.stderr.write(
                "Vas a poner synced_to_sheets_at = NULL en TODOS los tickets "
                f"de {settings.SQLITE_PATH}. Esto fuerza re-sync masivo. "
                "Confirma escribiendo 'si': "
            )
            sys.stderr.flush()
            ans = input().strip().lower()
            if ans != "si":
                sys.stderr.write("Cancelado.\n")
                return 1
        affected = _reset_sync(settings.SQLITE_PATH)
        sys.stderr.write(f"Reset OK: {affected} tickets marcados como pendientes.\n")

    client = SheetsClient(
        spreadsheet_id=settings.GSHEETS_SPREADSHEET_ID,
        credentials_path=settings.GSHEETS_CREDENTIALS_PATH,
        worksheet_name=settings.GSHEETS_WORKSHEET_NAME,
    )
    sync = SheetsSync(client=client, settings=settings)

    sys.stderr.write(
        f"Sincronizando -> spreadsheet={settings.GSHEETS_SPREADSHEET_ID} "
        f"worksheet={settings.GSHEETS_WORKSHEET_NAME!r} max={args.max}\n"
    )
    subidos = sync.run_once(max_results=args.max)
    print(f"OK subidos={subidos}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
