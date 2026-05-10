"""Punto de entrada de la aplicacion FastAPI.

Expone:

- ``/health`` para el HEALTHCHECK del contenedor (Paso 2).
- Las rutas web (``/``, ``/login``, ``/logout``, ``/tickets``) montadas desde
  ``app.web.routes`` (Paso 5a; el listado real llega en 5b).
"""

from fastapi import FastAPI

from app.web.routes import router as web_router

app = FastAPI(
    title="Telycoposio Tickets",
    version="0.1.0",
    description="Sistema interno de gestion de tickets de atencion al cliente.",
)


@app.get("/health", tags=["infra"])
def health() -> dict[str, str]:
    """Healthcheck minimo. Lo usa el HEALTHCHECK del contenedor."""
    return {"status": "ok"}


app.include_router(web_router)
