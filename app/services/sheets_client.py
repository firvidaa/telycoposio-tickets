"""Cliente fino sobre gspread para la sincronizacion con Google Sheets.

Aisla la dependencia de gspread del resto del codigo: ``sheets_sync.py``
y los tests inyectan dobles que implementan la misma superficie minima
(``col_values``, ``update``, ``batch_update``, ``append_rows``).

Decisiones:

- **gspread, no google-api-python-client.** Decidido en el Paso 9: para un
  upsert sobre una sola pestaña, gspread cubre con menos LOC. La API de
  bajo nivel solo aportaria flexibilidad que no usamos (charts, formato
  condicional, etc.).

- **Conexion por operacion** (igual que ``email_client``). Para 200
  tickets/mes y un ciclo cada 5 min, abrir y cerrar es trivial. Si en el
  futuro el volumen crece o gspread tarda mucho en autenticar, podemos
  cachear el handle del worksheet.

- **``USER_ENTERED`` como ``valueInputOption``.** Asi Sheets interpreta
  ``TRUE``/``FALSE`` como casillas booleanas nativas y ``0.92`` como
  numero. Si en el futuro detectamos formula injection desde emails de
  cliente (improbable), se anyade sanitizacion en ``sheets_formatter``.

- **Headers escritos solo si la hoja esta vacia.** No reescribimos si el
  operador ha tocado a mano. Cambiar el orden de las cabeceras requiere
  intervencion humana en la hoja (decision deliberada).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol

from app.services.sheets_formatter import HEADERS


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Excepciones
# ---------------------------------------------------------------------------


class SheetsClientError(RuntimeError):
    """Error generico del cliente de Sheets.

    El ``sheets_sync`` lo captura y lo convierte en log WARNING/ERROR. No
    propaga al scheduler.
    """


# ---------------------------------------------------------------------------
# Protocolo del worksheet (lo que el cliente necesita)
# ---------------------------------------------------------------------------


class WorksheetLike(Protocol):
    """Subset minimo de ``gspread.Worksheet`` que usamos.

    Los tests doblan un objeto que implementa estos metodos. gspread real
    los provee con la misma firma.
    """

    def col_values(self, col: int) -> list[str]: ...

    def update(self, range_name: str, values: list[list[str]], **kwargs: Any) -> Any: ...

    def batch_update(self, data: list[dict[str, Any]], **kwargs: Any) -> Any: ...

    def append_rows(
        self, values: list[list[str]], value_input_option: str = "RAW", **kwargs: Any
    ) -> Any: ...


#: Factory que abre la spreadsheet y devuelve el worksheet (creandolo si
#: hace falta). Inyectable para tests. El default usa gspread real.
WorksheetFactory = Callable[[], WorksheetLike]


# ---------------------------------------------------------------------------
# Factory default: gspread real
# ---------------------------------------------------------------------------


def _default_worksheet_factory(
    *,
    credentials_path: str,
    spreadsheet_id: str,
    worksheet_name: str,
) -> WorksheetLike:
    """Abre la worksheet via gspread con un service account.

    Import diferido para que los tests que no tocan Sheets no paguen el
    coste de importar gspread (y para no fallar si gspread no esta
    instalado en algun entorno minimo de CI).
    """
    import gspread  # type: ignore[import-untyped]

    try:
        client = gspread.service_account(filename=credentials_path)
        spreadsheet = client.open_by_key(spreadsheet_id)
    except FileNotFoundError as exc:
        raise SheetsClientError(
            f"credenciales no encontradas en {credentials_path!r}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise SheetsClientError(
            f"no se pudo abrir spreadsheet_id={spreadsheet_id!r}: {type(exc).__name__}"
        ) from exc

    try:
        return spreadsheet.worksheet(worksheet_name)
    except Exception:  # gspread.WorksheetNotFound u otras
        try:
            return spreadsheet.add_worksheet(
                title=worksheet_name, rows=1000, cols=len(HEADERS)
            )
        except Exception as exc:  # noqa: BLE001
            raise SheetsClientError(
                f"no se pudo crear worksheet {worksheet_name!r}: {type(exc).__name__}"
            ) from exc


# ---------------------------------------------------------------------------
# SheetsClient
# ---------------------------------------------------------------------------


class SheetsClient:
    """Wrapper sobre gspread con tres operaciones de alto nivel.

    Inyectables:

    - ``worksheet_factory``: callable sin args que devuelve un objeto con
      la superficie de ``WorksheetLike``. Default = gspread real
      construido a partir de ``credentials_path`` + ``spreadsheet_id`` +
      ``worksheet_name``.

    Los tests pasan un ``worksheet_factory`` que devuelve un fake
    worksheet y nunca tocan gspread.
    """

    def __init__(
        self,
        *,
        spreadsheet_id: str,
        credentials_path: str,
        worksheet_name: str,
        worksheet_factory: WorksheetFactory | None = None,
    ) -> None:
        self._spreadsheet_id = spreadsheet_id
        self._credentials_path = credentials_path
        self._worksheet_name = worksheet_name
        if worksheet_factory is None:
            self._factory: WorksheetFactory = lambda: _default_worksheet_factory(
                credentials_path=credentials_path,
                spreadsheet_id=spreadsheet_id,
                worksheet_name=worksheet_name,
            )
        else:
            self._factory = worksheet_factory

    # ----------------------------------------------------------- API publica

    def read_state(self) -> tuple[WorksheetLike, dict[str, int]]:
        """Abre la hoja, escribe headers si esta vacia, devuelve ``(ws, id->row)``.

        El indice ``row`` es 1-based y excluye la fila de cabeceras: si la
        hoja tiene ``[HEADERS, TLY-2026-0001, TLY-2026-0002]``, el mapa es
        ``{"TLY-2026-0001": 2, "TLY-2026-0002": 3}``.

        Si la hoja esta vacia, escribe HEADERS y devuelve un mapa vacio.
        Si la primera fila no coincide con HEADERS, **no** la sobreescribe
        (el operador puede haber renombrado a mano); loguea WARNING y
        sigue adelante usando el mapa de IDs tal cual.
        """
        try:
            ws = self._factory()
        except SheetsClientError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SheetsClientError(
                f"abrir worksheet fallo: {type(exc).__name__}"
            ) from exc

        try:
            col_a = ws.col_values(1)
        except Exception as exc:  # noqa: BLE001
            raise SheetsClientError(
                f"leer columna A fallo: {type(exc).__name__}"
            ) from exc

        if not col_a:
            # Hoja vacia: escribimos cabeceras y devolvemos mapa vacio.
            try:
                ws.update(
                    "A1",
                    [list(HEADERS)],
                    value_input_option="USER_ENTERED",
                )
            except Exception as exc:  # noqa: BLE001
                raise SheetsClientError(
                    f"escribir cabeceras fallo: {type(exc).__name__}"
                ) from exc
            return ws, {}

        if tuple(col_a[:1]) and col_a[0] != HEADERS[0]:
            # La primera celda no es "ID". El operador puede haber tocado.
            # No sobreescribimos. Avisamos en log y seguimos asumiendo que
            # las filas a partir de la 2 contienen IDs en columna A.
            logger.warning(
                "sheets_client: primera celda inesperada %r (se esperaba %r). "
                "No reescribo cabeceras; continuo con upserts.",
                col_a[0],
                HEADERS[0],
            )

        id_to_row: dict[str, int] = {}
        # Saltamos la fila 1 (cabeceras). Las filas validas tienen un ID.
        for idx, value in enumerate(col_a[1:], start=2):
            if value:
                id_to_row[value] = idx
        return ws, id_to_row

    def apply_changes(
        self,
        ws: WorksheetLike,
        *,
        updates: list[tuple[int, list[str]]],
        appends: list[list[str]],
    ) -> None:
        """Aplica updates (por numero de fila) y appends. Una llamada por bloque.

        Si tanto ``updates`` como ``appends`` estan vacios, no hace nada.

        Cualquier excepcion del API se envuelve en ``SheetsClientError``
        para que ``sheets_sync`` la maneje uniformemente.
        """
        if updates:
            try:
                ws.batch_update(
                    [
                        {
                            "range": f"A{row_idx}",
                            "values": [values],
                        }
                        for row_idx, values in updates
                    ],
                    value_input_option="USER_ENTERED",
                )
            except Exception as exc:  # noqa: BLE001
                raise SheetsClientError(
                    f"batch_update fallo: {type(exc).__name__}"
                ) from exc

        if appends:
            try:
                ws.append_rows(appends, value_input_option="USER_ENTERED")
            except Exception as exc:  # noqa: BLE001
                raise SheetsClientError(
                    f"append_rows fallo: {type(exc).__name__}"
                ) from exc
