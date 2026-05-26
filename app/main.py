"""Punto de entrada de la aplicacion FastAPI.

Expone:

- ``/health`` para el HEALTHCHECK del contenedor (Paso 2).
- Las rutas web (``/``, ``/login``, ``/logout``, ``/tickets``, ``/tickets/{id}``)
  montadas desde ``app.web.routes`` (Paso 5a + 5b).

El handler de ``SessionRequiredError`` se registra a nivel de app: cuando una
ruta protegida con ``require_user_html`` recibe una request sin sesion valida,
la excepcion se traduce a un 303 hacia ``/login``. Asi cualquier ruta nueva
con esa dependency queda protegida automaticamente sin codigo de auth manual.

**Lifespan (Pasos 8 y 9).** Arrancamos un unico ``AsyncIOScheduler`` con
dos posibles jobs independientes:

- ``email_poller`` (Paso 8): activo si ``WORKER_ENABLED=true``. Llama a
  ``EmailPoller.run_once`` cada ``EMAIL_POLL_INTERVAL_SECONDS``.
- ``sheets_sync`` (Paso 9): activo si ``GSHEETS_ENABLED=true``. Llama a
  ``SheetsSync.run_once`` cada ``GSHEETS_SYNC_INTERVAL_SECONDS``.

Ambos jobs son sincronos y se envuelven en ``asyncio.to_thread`` para no
bloquear el event loop. ``max_instances=1`` + ``coalesce=True`` evita
solapes y acumulacion de jobs si una iteracion tarda mas que el
intervalo. Si ninguno de los dos flags esta activo, el scheduler no se
crea y el lifespan es un no-op (caso por defecto en tests y dev).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.services.email_client import EmailClient
from app.services.sheets_client import SheetsClient
from app.services.sheets_sync import SheetsSync
from app.web.routes import (
    SessionRequiredError,
    http_exception_handler,
    router as web_router,
    session_required_handler,
    validation_exception_handler,
)
from app.workers.email_poller import EmailPoller


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Arranca y para el scheduler segun los flags de cada job.

    En tests/dev sin Gmail/Sheets real, ambos flags estan en ``False``
    (default) y este lifespan es un no-op.
    """
    settings = get_settings()
    scheduler: AsyncIOScheduler | None = None
    jobs: list[str] = []

    if settings.WORKER_ENABLED:
        email_client = EmailClient(settings=settings)
        poller = EmailPoller(email_client=email_client, settings=settings)

        async def _run_poller_job() -> None:
            # El poller es sincrono y abre/cierra conexiones IMAP, SMTP y
            # SQLite. Lo aislamos en un thread para no bloquear el event
            # loop ni el resto de las rutas HTTP.
            await asyncio.to_thread(poller.run_once)

        if scheduler is None:
            scheduler = AsyncIOScheduler()
        scheduler.add_job(
            _run_poller_job,
            trigger="interval",
            seconds=settings.EMAIL_POLL_INTERVAL_SECONDS,
            id="email_poller",
            max_instances=1,
            coalesce=True,
        )
        jobs.append(
            f"email_poller@{settings.EMAIL_POLL_INTERVAL_SECONDS}s"
        )

    if settings.GSHEETS_ENABLED:
        # El validador de ``Settings`` ya garantiza que estos dos no son
        # None cuando GSHEETS_ENABLED=true, asi que el ``assert`` es solo
        # un narrowing para el type checker.
        assert settings.GSHEETS_SPREADSHEET_ID is not None
        assert settings.GSHEETS_CREDENTIALS_PATH is not None
        sheets_client = SheetsClient(
            spreadsheet_id=settings.GSHEETS_SPREADSHEET_ID,
            credentials_path=settings.GSHEETS_CREDENTIALS_PATH,
            worksheet_name=settings.GSHEETS_WORKSHEET_NAME,
        )
        sheets_sync = SheetsSync(client=sheets_client, settings=settings)

        async def _run_sheets_job() -> None:
            await asyncio.to_thread(sheets_sync.run_once)

        if scheduler is None:
            scheduler = AsyncIOScheduler()
        scheduler.add_job(
            _run_sheets_job,
            trigger="interval",
            seconds=settings.GSHEETS_SYNC_INTERVAL_SECONDS,
            id="sheets_sync",
            max_instances=1,
            coalesce=True,
        )
        jobs.append(
            f"sheets_sync@{settings.GSHEETS_SYNC_INTERVAL_SECONDS}s"
        )

    if scheduler is not None:
        scheduler.start()
        logger.info("scheduler arrancado con jobs=%s", jobs)

    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
            logger.info("scheduler detenido")


app = FastAPI(
    title="Telycoposio Tickets",
    version="0.1.0",
    description="Sistema interno de gestion de tickets de atencion al cliente.",
    lifespan=lifespan,
)


@app.get("/health", tags=["infra"])
def health() -> dict[str, str]:
    """Healthcheck minimo. Lo usa el HEALTHCHECK del contenedor."""
    return {"status": "ok"}


app.add_exception_handler(SessionRequiredError, session_required_handler)  # type: ignore[arg-type]
app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
app.include_router(web_router)
