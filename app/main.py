"""Punto de entrada de la aplicacion FastAPI.

Expone:

- ``/health`` para el HEALTHCHECK del contenedor (Paso 2).
- Las rutas web (``/``, ``/login``, ``/logout``, ``/tickets``, ``/tickets/{id}``)
  montadas desde ``app.web.routes`` (Paso 5a + 5b).

El handler de ``SessionRequiredError`` se registra a nivel de app: cuando una
ruta protegida con ``require_user_html`` recibe una request sin sesion valida,
la excepcion se traduce a un 303 hacia ``/login``. Asi cualquier ruta nueva
con esa dependency queda protegida automaticamente sin codigo de auth manual.
"""

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.web.routes import (
    SessionRequiredError,
    http_exception_handler,
    router as web_router,
    session_required_handler,
    validation_exception_handler,
)

app = FastAPI(
    title="Telycoposio Tickets",
    version="0.1.0",
    description="Sistema interno de gestion de tickets de atencion al cliente.",
)


@app.get("/health", tags=["infra"])
def health() -> dict[str, str]:
    """Healthcheck minimo. Lo usa el HEALTHCHECK del contenedor."""
    return {"status": "ok"}


app.add_exception_handler(SessionRequiredError, session_required_handler)  # type: ignore[arg-type]
app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
app.include_router(web_router)
