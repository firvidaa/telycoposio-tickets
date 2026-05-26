# HANDOFF — Telycoposio Tickets

Documento puente para retomar el proyecto en otro equipo, tras una pausa
larga, o para que una sesion de Claude Code arranque con contexto.

No sustituye a:

- [`CLAUDE.md`](./CLAUDE.md) — contexto operativo permanente (stack,
  convenciones, decisiones de diseno).
- [`INICIO.md`](./INICIO.md) — cheatsheet de comandos diarios.
- [`SPEC.md`](./SPEC.md) — especificacion funcional (fuente de verdad
  del alcance, v1.3).

Este fichero responde a "donde estabamos y como retomo".

Ultima actualizacion: **2026-05-27**. Branch: `master`. HEAD: `c9eede4`.

---

## 1. Objetivo del proyecto

Sistema interno de gestion de tickets de atencion al cliente para
Telycoposio (empresa espanola de 3 personas, ~200 tickets/mes).
Centraliza emails entrantes en tickets numerados (`TLY-AAAA-NNNN`)
categorizados con IA. Despliegue local + VPN, sin trafico publico.

Stack: Python 3.11+, FastAPI + Jinja2 + Tailwind, SQLite (verdad
operativa), Google Sheets (espejo visible), IMAP+SMTP con App Password,
Anthropic Claude Haiku 4.5.

---

## 2. Estado actual

### 2.1. Pasos completados

| Paso | Descripcion | Commit |
|------|-------------|--------|
| 1 | Estructura, deps, config base | `8a84967` |
| 2 | Dockerfile + docker-compose + `/health` | `21cbb87` |
| 3 | Modelo de datos, SQLite, init scripts | `92404f2` |
| 4 | `app.config` centralizado + ticket_service | `502ce59` |
| 5a | Auth + login/logout + rate limit | `1b60b26` |
| 5b | Listado y detalle de tickets | `28a29d6` |
| 6 | Clasificador Anthropic con tool use forzado | `af54621` |
| 7 | Cliente IMAP/SMTP con App Password (v1.3) | `fc35b08` |
| 8 | Worker APScheduler con idempotencia completa | `162bc22` |
| 9 | Sync unidireccional SQLite -> Google Sheets | `40ee40c` |
| 10 | Respuestas al cliente desde la web (`In-Reply-To`) | `c9eede4` |

### 2.2. Pasos pendientes

- **Paso 11** — Despliegue real + backups. **Siguiente paso.**

### 2.3. Suite de tests

**283 verdes, 2 deselected** (los `real_api` se excluyen por defecto).

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Para tests contra APIs reales (Anthropic, Gmail real, coste / latencia):

```powershell
.\.venv\Scripts\python.exe -m pytest -m real_api
```

---

## 3. Migracion a otro equipo

### 3.1. Lo que SI esta en el repo

Todo el codigo, tests, schema SQL, scripts, plantillas, docs. Con
`git clone` se obtiene un proyecto reproducible.

### 3.2. Lo que NO esta en el repo (gitignored)

Hay que llevarlos a mano al equipo nuevo:

| Path | Que es | Como recuperar |
|------|--------|----------------|
| `.env` | Variables de entorno (incluye `APP_SECRET_KEY`, `EMAIL_APP_PASSWORD`) | Copiar del equipo origen via medio seguro (USB cifrado, gestor de passwords, NUNCA email/chat). Recrear desde `.env.example` si se prefiere. |
| `secrets/gsheets_service_account.json` | Credenciales del service account de Google Cloud para Sheets | Copiar del equipo origen, **o** descargar de nuevo desde GCP (Consola -> Service accounts -> Keys). |
| `data/app.db` | SQLite con tickets y usuarios actuales | Copiar si se quiere preservar historial; en su defecto recrear vacia con `init_db.py` + `add_user.py`. |

### 3.3. Pasos en el equipo nuevo

```powershell
git clone <url-del-repo>
cd telycoposio-tickets

# 1. venv y deps
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# 2. Copiar a mano .env y secrets/ desde el equipo origen
# (NO compartirlos por chat/email)

# 3. BD: o copias data/app.db existente, o creas una nueva:
.\.venv\Scripts\python.exe scripts/init_db.py
.\.venv\Scripts\python.exe scripts/add_user.py --username <user> --display-name "<Nombre>" --role admin

# 4. Tests verdes desde el primer momento:
.\.venv\Scripts\python.exe -m pytest -q
# Esperado: 283 passed, 2 deselected.

# 5. SI vas a activar el poller por primera vez contra una cuenta IMAP,
#    LEER INICIO.md §9.1 PRIMERO y correr el bootstrap:
.\.venv\Scripts\python.exe scripts/email_smoke.py --mark-all-existing-processed --yes

# 6. Arrancar:
.\.venv\Scripts\uvicorn.exe app.main:app --reload
```

### 3.4. Cosas que NO hay que recrear

- **App Password de Gmail**: no esta ligada al equipo. Sigue funcionando
  desde la nueva maquina sin tocar nada en Google.
- **Service account de Google Sheets**: idem, las credenciales son
  validas desde cualquier maquina mientras el JSON este intacto.
- **Hoja de Sheets compartida**: ya esta compartida con el service
  account, no hace falta volver a compartir.

---

## 4. Trampas que NO se ven en el codigo (lecciones de la ultima sesion)

Resumidas en [`INICIO.md`](./INICIO.md) §9, ampliadas aqui:

### 4.1. Bootstrap obligatorio del worker

`WORKER_ENABLED=true` hace que el poller considere **todos los emails
sin `TLY_PROCESSED`** como nuevos. Si el INBOX tiene historicos
(newsletters, notificaciones, smoke tests), los procesara todos y
mandara auto-respuestas a remitentes que no las esperan (riesgo de
bucles con auto-responders).

**Solucion:** `email_smoke.py --mark-all-existing-processed --yes`
antes del primer arranque del worker en cualquier cuenta IMAP nueva.

Se descubrio en esta sesion porque no se documento en el flujo del
Paso 10. Ahora esta en INICIO.md §9.1.

### 4.2. `seed_demo` usa IDs altos

`scripts/seed_demo_tickets.py` inserta en `TLY-2026-9991..9997`. Al
recibir un email real, el contador anual se agota muy rapido (despues
del segundo email real `next_id` intenta `10000` y peta con
`TicketIdOverflowError`).

**Solucion temporal:** no usar `seed_demo` en BDs que reciban emails
reales. **Deuda tecnica:** mover los demo a `DEMO-NNNN` o IDs bajos.
No se arreglo en el Paso 10 porque es trabajo del Paso 5b.

### 4.3. `client_email` del service account hay que compartirlo con la hoja

El service account tiene su propio email
(`*@*.iam.gserviceaccount.com`); la hoja debe estar compartida con
permiso de Editor para que `gspread` pueda escribir. Si se cambia el
service account hay que recompartir.

Service account actual:
`telycoposio-sheets-sync@telycoposio-tickets.iam.gserviceaccount.com`

Hoja activa:
`https://docs.google.com/spreadsheets/d/1upuwJ5I-Dm5oVweYZOYr1EJ0W5jSRbdojP4rAD63rDE/`

Pestaña: `Tickets` (la crea el cliente si no existe; al hacerlo
aparece junto a la "Hoja 1" por defecto).

### 4.4. `Python 3.14.4 (local) vs 3.11 (Dockerfile)`

Drift conocido y deliberadamente ignorado. CLAUDE.md §6 lo apunta.
Si se introduce sintaxis 3.12+ habria que actualizar el Dockerfile.

---

## 5. Trabajo reciente (sesiones Paso 9 y 10)

Para no obligar al lector a hacer `git log -p`, resumen de los dos
ultimos commits funcionales:

### 5.1. Paso 9 (commit `40ee40c`)

Sync unidireccional SQLite -> Google Sheets. SQLite es la verdad;
Sheets es solo vista. El sync corre dentro del scheduler de
APScheduler cada `GSHEETS_SYNC_INTERVAL_SECONDS` (default 5 min).

Ficheros clave:

- [app/services/sheets_formatter.py](app/services/sheets_formatter.py) — funciones puras, sin red.
- [app/services/sheets_client.py](app/services/sheets_client.py) — wrapper sobre gspread.
- [app/services/sheets_sync.py](app/services/sheets_sync.py) — orquestador (idempotente via `synced_to_sheets_at`).
- [scripts/sheets_smoke.py](scripts/sheets_smoke.py) — CLI para forzar `run_once()` manualmente.
- Modificado: [app/main.py](app/main.py) (lifespan con dos jobs independientes), [app/config.py](app/config.py) (vars `GSHEETS_*` + validador que falla si `GSHEETS_ENABLED=true` sin ID/credenciales).

54 tests nuevos (formatter + client + sync).

### 5.2. Paso 10 (commit `c9eede4`)

Respuestas al cliente desde la web con `In-Reply-To` para mantener
hilo. Tabla nueva `ticket_replies` con `ON DELETE CASCADE` desde
`tickets`. UI en la pagina del detalle del ticket.

Ficheros clave:

- [app/models/reply.py](app/models/reply.py) — modelo `Reply` (inmutable).
- [app/services/reply_service.py](app/services/reply_service.py) — `send_reply()`: valida -> SMTP -> persiste + opcional cambio de estado, todo en una transaccion.
- [app/services/ticket_service.py](app/services/ticket_service.py) — anyade `get_replies()`.
- [app/web/routes.py](app/web/routes.py) — `POST /tickets/{id}/reply` (PRG en exito, re-render en error).
- [app/web/templates/ticket_detail.html](app/web/templates/ticket_detail.html) — historial + form (textarea + dropdown estado).
- [app/db/schema.sql](app/db/schema.sql) — tabla `ticket_replies` + indice + FK CASCADE.

31 tests nuevos (reply_service + rutas + schema).

### 5.3. Decisiones de diseno relevantes (no obvias del codigo)

- **Respuestas son inmutables**: nunca se editan ni se borran desde la
  UI. Si hay que corregir, se envia otra respuesta.
- **No persistir si SMTP falla**: misma filosofia que el poller del
  Paso 8. La ventana de "SMTP OK pero BD falla" es teorica, tradeoff
  aceptado.
- **No tocamos `synced_to_sheets_at` al cambiar el ticket**: el sync
  del Paso 9 detecta `last_updated_at > synced_to_sheets_at` y
  refresca la fila en Sheets automaticamente.
- **`_compute_reply_subject` no duplica `Re:`**: si el subject ya
  empieza por `Re:`/`RE:` (cualquier caja, con o sin espacio), no
  anyade otro.
- **Form deshabilitado si no se puede responder**: ticket sin
  `from_email`, sin `raw_message_id`, o canal != email muestran nota
  explicativa en lugar del form.

---

## 6. Siguiente paso: Paso 11

Despliegue real en el servidor de oficina + backups. Lo que **se
espera** (a confirmar al arrancar la siguiente sesion):

- `docker compose build` + `up -d` ya construye y arranca; verificar
  que `WORKER_ENABLED` y `GSHEETS_ENABLED` se setean desde el `.env`
  del servidor (NO el de dev).
- Volumenes: `./data` (SQLite) y `./secrets` (credenciales). Backup
  diario del fichero `app.db` a un destino externo (USB cifrado, otra
  maquina, S3 si hay).
- Estrategia de backup: SQLite admite `.backup` online; aceptable un
  cron diario + retencion N dias.
- Healthcheck del contenedor ya esta en el Dockerfile (`/health`).
  Verificar que Compose marca `healthy` antes de aceptar trafico.
- Acceso por VPN, sin reverse proxy publico (SPEC §2).

Antes de tocar codigo del Paso 11:

1. Releer SPEC.md §relevante sobre despliegue.
2. Decidir politica de backups con el usuario (retencion, destino).
3. Probar `docker compose build` localmente para detectar issues con
   `python:3.11-slim` y el drift Python 3.14 -> 3.11.
4. Verificar que las migraciones del schema (`init_schema`) se
   aplican al arrancar el contenedor en una BD nueva.
5. Confirmar con el usuario si el `bootstrap` del worker (§4.1) hay
   que ejecutarlo dentro o fuera del contenedor.

---

## 7. Para una sesion de Claude Code en el equipo nuevo

Si vas a continuar con Claude Code:

1. Abrir el repo en VSCode/Cursor.
2. CLAUDE.md se carga automaticamente en el contexto.
3. Empezar con: *"Lee HANDOFF.md y dime donde estamos. Quiero arrancar
   el Paso 11."*
4. La sesion sabra lo siguiente:
   - Estado del proyecto (este documento).
   - Decisiones tomadas (CLAUDE.md + commits).
   - Trampas operativas (§4 + INICIO.md §9).
   - Convenciones de codigo (CLAUDE.md §4) y de commits (uno por Paso,
     mensaje "Paso N: <breve>", sin Co-Authored-By en pasos N).
5. Recordar que la metodologia es: el usuario propone "Paso N", la
   sesion propone alcance + decisiones + plan de tests + ficheros,
   espera OK explicito, implementa, espera OK antes de commitear.
