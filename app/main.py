"""Punto de entrada de la aplicacion FastAPI.

En el Paso 2 solo se expone un endpoint `/health` para que Docker pueda
verificar el estado del contenedor. El resto de funcionalidad (rutas web,
worker de Gmail, etc.) se anadira en pasos posteriores.
"""

from fastapi import FastAPI

app = FastAPI(
    title="Telycoposio Tickets",
    version="0.1.0",
    description="Sistema interno de gestion de tickets de atencion al cliente.",
)


@app.get("/health", tags=["infra"])
def health() -> dict[str, str]:
    """Healthcheck minimo. Lo usa el HEALTHCHECK del contenedor."""
    return {"status": "ok"}
