# Imagen de produccion para telycoposio-tickets.
# El desarrollo se hace en venv local con `uvicorn --reload` (ver README);
# este Dockerfile esta pensado solo para el despliegue final en el servidor.

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# curl: necesario para el HEALTHCHECK contra /health.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Copiamos los metadatos y el codigo necesarios para que hatchling construya
# el wheel del paquete `app`.
COPY pyproject.toml README.md ./
COPY app/ ./app/

RUN pip install --no-cache-dir .

# Usuario sin privilegios + carpetas montables (data/secrets las monta compose).
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/data /app/secrets \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --fail --silent http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
