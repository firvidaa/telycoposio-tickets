"""Punto de entrada de la aplicacion FastAPI.

Expone:

- ``/health`` para el HEALTHCHECK del contenedor (Paso 2).
- Las rutas web (``/``, ``/login``, ``/logout``, ``/tickets``, ``/tickets/{id}``)
  montadas desde ``app.web.routes`` (Paso 5a + 5b).

El handler de ``SessionRequiredError`` se registra a nivel de app: cuando una
ruta protegida con ``require_user_html`` recibe una request sin sesion valida,
la excepcion se traduce a un 303 hacia ``/login``. Asi cualquier ruta nueva
con esa dependency queda protegida automaticamente sin codigo de auth manual.

**Lifespan (Paso 8).** Si ``WORKER_ENABLED=True``, arrancamos un
``AsyncIOScheduler`` con un unico job que invoca ``EmailPoller.run_once``
cada ``EMAIL_POLL_INTERVAL_SECONDS`` segundos. El job es sincrono y se
envuelve en ``asyncio.to_thread`` para no bloquear el event loop. Con
``max_instances=1`` + ``coalesce=True`` evitamos solapes y nunca
acumulamos jobs si una iteracion tarda mas que el intervalo. Por defecto
``WORKER_ENABLED=False`` para que ``uvicorn --reload`` y los tests no
toquen Gmail real sin querer.
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
    """Arranca y para el scheduler segun ``WORKER_ENABLED``.

    En tests/dev sin Gmail real, ``WORKER_ENABLED=False`` (default) y este
    lifespan es un no-op. Para encender el poller, ``WORKER_ENABLED=true``
    en ``.env``.
    """
    settings = get_settings()
    scheduler: AsyncIOScheduler | None = None

    if settings.WORKER_ENABLED:
        client = EmailClient(settings=settings)
        poller = EmailPoller(email_client=client, settings=settings)

        async def _run_poller_job() -> None:
            # El poller es sincrono y abre/cierra conexiones IMAP, SMTP y
            # SQLite. Lo aislamos en un thread para no bloquear el event
            # loop ni el resto de las rutas HTTP.
            await asyncio.to_thread(poller.run_once)

        scheduler = AsyncIOScheduler()
        scheduler.add_job(
            _run_poller_job,
            trigger="interval",
            seconds=settings.EMAIL_POLL_INTERVAL_SECONDS,
            id="email_poller",
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        logger.info(
            "scheduler email_poller arrancado: poll cada %ds",
            settings.EMAIL_POLL_INTERVAL_SECONDS,
        )

    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
            logger.info("scheduler email_poller detenido")


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
