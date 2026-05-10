# Telycoposio Tickets

Sistema interno de gestion de tickets de atencion al cliente para Telycoposio.

Centraliza las solicitudes entrantes (email, WhatsApp en Fase 2, buzon de voz en
Fase 3) en un unico sistema de tickets numerados y categorizados automaticamente
con IA (Claude Haiku).

> Para el detalle funcional, modelo de datos y plan de fases, ver [`SPEC.md`](./SPEC.md).
> Version actual del SPEC: **v1.2**.

---

## Stack

- **Python 3.11+** con FastAPI + Jinja2 + Tailwind (via CDN).
- **SQLite** local como fuente de verdad operativa.
- **Google Sheets** como capa visible para humanos (sincronizacion en background).
- **Gmail API** (OAuth 2.0) para entrada y salida de email.
- **Anthropic API** (Claude Haiku 4.5) para categorizar tickets.
- Despliegue final en **Docker Compose** sobre servidor de oficina.

---

## Estructura del proyecto

```
telycoposio-tickets/
├── README.md                 Este archivo
├── SPEC.md                   Especificacion funcional y tecnica (v1.2)
├── CHECKLIST_PREVIO.md       Pasos previos antes de empezar a codificar
├── PROMPT_BASE.md            Prompt base para sesiones con Claude Code
├── .env.example              Plantilla de variables de entorno
├── .gitignore
├── pyproject.toml            Dependencias y configuracion de herramientas
├── app/                      Codigo de la aplicacion (FastAPI)
│   ├── db/                   Acceso a SQLite y Google Sheets
│   ├── models/               Modelos de datos (Ticket, User, ...)
│   ├── services/             Logica de negocio (Gmail, classifier, auth, ...)
│   ├── workers/              Tareas en background (poller de Gmail)
│   ├── web/                  Rutas, templates Jinja y estaticos
│   └── utils/
├── scripts/                  Scripts auxiliares (init_db, add_user, ...)
├── tests/                    Tests
├── data/                     SQLite y datos persistentes (NO versionado)
└── secrets/                  Credenciales OAuth y service accounts (NO versionado)
```

---

## Arranque rapido (desarrollo)

Estrategia: **venv local con `uvicorn --reload`** para desarrollo del dia a dia.
Docker se usa solo para el despliegue final en el servidor de oficina.

1. Clonar el repo y entrar en la carpeta.
2. Crear y activar un entorno virtual:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
3. Instalar dependencias:
   ```powershell
   pip install -e ".[dev]"
   ```
4. Copiar `.env.example` a `.env` y rellenar valores (cuando haya servicios reales conectados).
5. Colocar las credenciales de Google en `secrets/` (no se versionan).
6. Inicializar la BD local:
   ```powershell
   python scripts/init_db.py
   ```
   Crea `data/app.db` con las tablas `tickets` y `users` (idempotente).
7. Anadir al menos un usuario para poder hacer login (cuando exista la web):
   ```powershell
   python scripts/add_user.py --username alfredo --display-name "Alfredo" --role admin
   ```
8. Arrancar el servidor:
   ```powershell
   uvicorn app.main:app --reload
   ```
9. Verificar que vive: `curl http://localhost:8000/health` -> `{"status":"ok"}`.

---

## Despliegue (produccion, servidor de oficina)

Solo accesible por red interna + VPN, sin reverse proxy en esta fase.

```bash
docker compose build
docker compose up -d
docker compose ps          # comprobar que el healthcheck pasa a "healthy"
```

El contenedor expone el puerto 8000 y monta `./data` (SQLite) y `./secrets`
(credenciales OAuth/service accounts) como volumenes.

---

## Estado actual

- [x] Paso 1 — Estructura del proyecto, dependencias y configuracion base.
- [x] Paso 2 — Dockerfile, docker-compose.yml y endpoint `/health`.
- [x] Paso 3 — Modelo de datos, SQLite y scripts de inicializacion (SPEC v1.2).
- [x] Paso 4 — Configuracion centralizada (`app.config`) y servicio de tickets (`app.services.ticket_service`).
- [x] Paso 5a — Auth (bcrypt + cookie firmada con `itsdangerous`), login/logout, rate limit.
- [ ] Paso 5b — Listado y detalle de tickets (read-only).

---

## Licencia

Software propietario de Telycoposio. Uso interno.
