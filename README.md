# Telycoposio Tickets

Sistema interno de gestion de tickets de atencion al cliente para Telycoposio.

Centraliza las solicitudes entrantes (email, WhatsApp en Fase 2, buzon de voz en
Fase 3) en un unico sistema de tickets numerados y categorizados automaticamente
con IA (Claude Haiku).

> Para el detalle funcional, modelo de datos y plan de fases, ver [`SPEC.md`](./SPEC.md).
> Version actual del SPEC: **v1.1**.

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
├── SPEC.md                   Especificacion funcional y tecnica (v1.1)
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

> En este Paso 1 todavia **no hay codigo** ejecutable: solo estructura, dependencias
> y configuracion. Las instrucciones siguientes son la forma final esperada.

1. Clonar el repo y entrar en la carpeta.
2. Crear y activar un entorno virtual:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
3. Instalar dependencias (cuando haya un lockfile o paquetes instalables):
   ```powershell
   pip install -e ".[dev]"
   ```
4. Copiar `.env.example` a `.env` y rellenar valores.
5. Colocar las credenciales de Google en `secrets/` (no se versionan).
6. Inicializar la BD local: `python scripts/init_db.py` (pendiente de implementar).
7. Arrancar el servidor: `uvicorn app.main:app --reload` (pendiente de implementar).

---

## Estado actual

- [x] Paso 1 — Estructura del proyecto, dependencias y configuracion base.
- [ ] Paso 2 — (pendiente).

---

## Licencia

Software propietario de Telycoposio. Uso interno.
