# CLAUDE.md

Contexto operativo para Claude Code. Leer esto al arrancar cualquier sesion
nueva: resume el proyecto, su estado y las convenciones que aplicamos al
escribir codigo aqui. La fuente de verdad funcional sigue siendo
[`SPEC.md`](./SPEC.md) (v1.3); este documento no la duplica, la complementa.

---

## 1. De que va el proyecto

Sistema interno de gestion de tickets de atencion al cliente para
**Telycoposio**, una pequena empresa espanola de 3 personas (venta de
material informatico/telecomunicaciones y soporte tecnico). Centraliza
solicitudes entrantes en tickets numerados (`TLY-AAAA-NNNN`) categorizados
con IA. Volumen objetivo: ~200 tickets/mes, 3 usuarios web concurrentes
maximo. Despliegue local + VPN, sin trafico publico.

Fases (ver SPEC.md §2):

- **Fase 1 (MVP, en curso):** Email + Web.
- **Fase 2:** WhatsApp Cloud API.
- **Fase 3:** Buzon de voz Aire Networks.

---

## 2. Estado actual

Pasos completados (cada uno = 1 commit en `master`):

- [x] **Paso 1** — Estructura, dependencias y configuracion base.
- [x] **Paso 2** — Dockerfile, docker-compose, `/health`.
- [x] **Paso 3** — Modelo de datos, SQLite, scripts de inicializacion.
- [x] **Paso 4** — `app.config` centralizado y `ticket_service`.
- [x] **Paso 5a** — Auth (bcrypt + cookie firmada), login/logout, rate limit.
- [x] **Paso 5b** — Listado y detalle de tickets (read-only) con filtros y
      paginacion.
- [x] **Paso 6** — Clasificador con Anthropic API (Claude Haiku 4.5),
      tool use forzado y defensa contra prompt injection.
- [x] **Paso 7** — Cliente IMAP/SMTP con App Password (sustituye Gmail API
      + OAuth, ver cambios v1.3 en SPEC).
- [x] **Paso 8** — Worker de polling con APScheduler, idempotencia completa
      y manejo de fallos.

Pendiente:

- [ ] **Paso 9** — Sincronizacion con Google Sheets (alcance por consensuar).
- [ ] **Paso 10** — Respuestas al cliente desde la web (SMTP con `In-Reply-To`).
- [ ] **Paso 11** — Despliegue real + backups.

Tests: ~193 verdes con `pytest tests/ -v`. Los marcados `real_api` (clasificador
y cliente de email contra cuentas reales) estan **excluidos por defecto**;
se ejecutan con `pytest -m real_api`.

---

## 3. Stack confirmado

| Capa | Eleccion |
|---|---|
| Lenguaje | Python 3.11+ (`requires-python = ">=3.11"` en pyproject) |
| Web | FastAPI + Jinja2 + Tailwind via CDN |
| Auth | Cookies firmadas con `itsdangerous` + bcrypt para passwords |
| BD operativa | SQLite local (`data/app.db`), fuente de verdad |
| BD visible | Google Sheets (espejo unidireccional SQLite -> Sheets) |
| Email | IMAP + SMTP con App Password Google (no Gmail API, no OAuth) |
| IA | Anthropic API — `claude-haiku-4-5-20251001` con tool use forzado |
| Worker | APScheduler in-process, arrancado desde el lifespan de FastAPI |
| Despliegue | Docker Compose en servidor de oficina (Linux), acceso por VPN |

### 3.1. Decisiones globales que no son evidentes leyendo el codigo

- **SQLite es fuente de verdad** (v1.2). Sheets se sincroniza desde SQLite
  en background; si Sheets cae, la app sigue operativa y el ticket queda
  con `synced_to_sheets_at = NULL`.
- **Worker en el mismo proceso que la web** (Paso 8). Para 200 tickets/mes
  separarlo seria sobreingenieria. `AsyncIOScheduler` con
  `max_instances=1` + `coalesce=True` evita solapes; el job sincrono se
  envuelve en `asyncio.to_thread(...)` para no bloquear el event loop.
- **Idempotencia por capas, no transaccional.** El poller asume que cada
  capa absorbe reintentos: `raw_message_id` UNIQUE en BD,
  `client_notified_at` como gate de la confirmacion, `TLY_PROCESSED`
  IMAP keyword fuera del bucle. La unica ventana conocida (SMTP-OK -> UPDATE
  client_notified_at) puede causar una doble confirmacion en un crash
  exacto; tradeoff aceptado frente a 2PC.
- **WORKER_ENABLED=false por defecto.** El scheduler no arranca hasta que
  se setea en `.env` a `true`. Asi `uvicorn --reload` y la suite de tests
  jamas tocan Gmail real sin querer.
- **Categoria puede ser NULL.** Si la IA falla o devuelve confianza
  `< 0.7`, `needs_review = True` y un humano revisara desde la UI. El
  clasificador **nunca lanza**; devuelve `ClassificationResult.failed()`.
- **Sin sesiones en BD** (v1.2). Cookies `HttpOnly + SameSite=Lax`
  firmadas con `APP_SECRET_KEY`. Invalidacion masiva = rotar la clave +
  reiniciar.
- **App Password en lugar de OAuth** (v1.3). El scope `gmail.modify` es
  restricted y exige verificacion CASA. Para 3 usuarios + bajo volumen es
  desproporcionado. Riesgo conocido: si Google retira App Passwords,
  toca migrar a Workspace + OAuth.

---

## 4. Convenciones de codigo

### 4.1. Texto

- **Comentarios, docstrings, logs y nombres de tests: castellano sin
  tildes** (ASCII). Convencion ya establecida en todo el codebase.
  Ejemplo: `# Configuracion centralizada`, no `# Configuración`.
- **Texto que va a un cliente real** (asuntos y cuerpos de email enviados
  desde la app) tambien va en ASCII por consistencia. Cualquier cambio a
  esto deberia ser una decision explicita.
- Sin emojis en codigo, docstrings ni logs.

### 4.2. Estilo Python

- **Type hints exhaustivos.** `from __future__ import annotations` en
  todos los modulos. Generics y unions usando sintaxis nativa
  (`str | None`, `list[str]`).
- **`@dataclass(frozen=True)` para modelos de dominio** (ver
  `app/models/ticket.py`). Inmutables; las "actualizaciones" generan
  copias.
- **kwargs-only en APIs publicas** (constructores, factory functions).
  Ejemplo: `EmailPoller(*, email_client, settings=None, ...)`.
- **Inyectables tipados.** Servicios reciben sus dependencias por
  constructor (callable o instancia) con defaults razonables. Tests
  inyectan dobles. No hacemos monkeypatch de modulos.
- **`Final` para constantes module-level** (`MAX_FAILURES_PER_UID: Final[int] = 5`).
- **`lru_cache` para singletons** (ver `get_settings`). En tests se
  llama a `get_settings.cache_clear()` en una fixture autouse.

### 4.3. Errores y logging

- **Capas externas no lanzan al scheduler ni al event loop.** El
  clasificador devuelve `ClassificationResult.failed()`; el poller
  captura `Exception` (con `# noqa: BLE001` y un log) para que un fallo
  imprevisto en un email no atasque los demas.
- **Granularidad de logs:**
  - `INFO`: hitos de exito ("poller exito uid=X ticket=Y").
  - `WARNING`: fallos transitorios esperables (IMAP momentaneamente
    caido, un email roto reintentable).
  - `ERROR`: fallos graves o que requieren atencion humana (UID
    declarado toxico tras N reintentos).
  - `exception(...)` solo en los catch defensivos genericos.
- **Nunca logear datos sensibles**: passwords, body completo de emails,
  cookies. Previews truncados (500 chars) son aceptables.
- **Mensajes de error accionables.** Si falta una env var critica, el
  log/stderr debe indicar exactamente el comando para arreglarlo (ver
  `_SECRET_KEY_HINT` en `config.py`).

### 4.4. Tests

- **Pytest, sin frameworks adicionales.** Nombres en castellano:
  `test_<accion>_<resultado_esperado>`.
- **SQLite real (en `tmp_path`), no mocks de BD.** Cada test inicializa
  el esquema con `init_schema()`.
- **Reloj congelado** (`_FROZEN_NOW`) y callables inyectados para
  determinismo.
- **Una asercion por concepto.** Tests cortos, scope claro.
- **Marker `real_api`** para los tests que tiran contra Anthropic/Gmail
  real. Excluidos por defecto en `pyproject.toml`
  (`addopts = "-m 'not real_api'"`).

### 4.5. Configuracion

- **Todas las env vars pasan por `app.config.Settings`.** No leer
  `os.environ` en otros modulos.
- **Defaults razonables para entorno local**, override en `.env` de
  produccion. Variables criticas (las 4: `APP_SECRET_KEY`,
  `ANTHROPIC_API_KEY`, `EMAIL_ADDRESS`, `EMAIL_APP_PASSWORD`) no tienen
  default; la app no arranca si faltan.
- **Validators de pydantic para normalizar input** del `.env`
  (ej.: `APP_BASE_URL` strippea trailing slashes).

### 4.6. Git y commits

- **Mensaje de commit: `Paso N: <descripcion breve>`** (asi viene
  dictado por el flujo de trabajo).
- **Un paso = un commit.** No fragmentar; no agrupar varios pasos.
- **No usar `--amend`** sobre commits ya creados; en cambio crear un
  commit nuevo si hay que corregir algo.
- **No usar `--no-verify`** ni saltarse hooks.
- Branch de trabajo: `master`. Branch principal de PRs: `main`.

---

## 5. Como trabajamos (flujo paso a paso)

1. El usuario plantea un "Paso N: <objetivo>".
2. Yo propongo: (a) alcance, (b) decisiones de diseno con tradeoffs,
   (c) plan de tests, (d) lista concreta de archivos a tocar.
3. El usuario revisa, ajusta y da el visto bueno **antes** de que yo
   toque codigo.
4. Implemento, corro `pytest tests/ -v`, reporto resultado.
5. **Me paro y espero confirmacion.** No commiteo por iniciativa propia.
6. Tras el OK explicito, hago el commit con el mensaje "Paso N: ...".
7. **No empiezo el Paso N+1 hasta que el usuario lo pida explicitamente.**

Comunicacion: en castellano, breve, sin floritura, sin emojis.

---

## 6. Entorno de desarrollo

- **Desarrollo: venv local con `uvicorn --reload`**. NO usar Docker para
  el ciclo dia-a-dia.
- **Docker es solo para el despliegue final** en el servidor de oficina
  (Linux). `docker compose build/up` se sugiere solo cuando el cambio
  toca el Dockerfile o las dependencias.
- **Maquina de desarrollo: Windows + PowerShell.** Cuidado con rutas,
  scripts shell, y comandos especificos de SO.
- **Python local: 3.14.4. Dockerfile: `python:3.11-slim`.** Drift
  conocido y anotado para revisar; no arreglar por iniciativa propia.
  Si se introduce sintaxis 3.12+, avisar.
- **Comando de tests:** `.\.venv\Scripts\python.exe -m pytest -q` (suite
  por defecto, sin red, sin coste).

### 6.1. Scripts auxiliares disponibles

```powershell
python scripts/init_db.py               # crea data/app.db (idempotente)
python scripts/add_user.py --username X --display-name "X" --role admin
python scripts/seed_demo_tickets.py     # tickets demo para UI
python scripts/classify_text.py --subject "..." --text "..."  # cuesta < 0.001 EUR
python scripts/email_smoke.py --list    # IMAP contra cuenta real
python scripts/email_smoke.py --mark-all-existing-processed   # bootstrap pre-Paso 8
```

---

## 7. Donde mirar primero

- **Alcance, modelo de datos, fases:** `SPEC.md` (v1.3).
- **Arranque y comandos:** `README.md`.
- **Configuracion centralizada:** `app/config.py`.
- **Flujo end-to-end del email:** `app/workers/email_poller.py` (Paso 8).
- **Cliente IMAP/SMTP:** `app/services/email_client.py` (Paso 7).
- **Clasificador IA:** `app/services/classifier.py` (Paso 6).
- **Negocio de tickets:** `app/services/ticket_service.py` (Paso 4).
- **Auth y rutas web:** `app/services/auth.py`, `app/web/routes.py`
  (Pasos 5a/5b).
- **Tests por dominio:** `tests/test_<modulo>.py`.

Si una afirmacion en este documento contradice el codigo, gana el
codigo; este doc debe actualizarse en el mismo commit que introduce
el cambio.
