# Inicio rapido y recordatorios

Cheatsheet operativo para el dia a dia. Asume que el proyecto ya esta
instalado (venv creado, deps instaladas, `.env` rellenado, BD
inicializada). Para la instalacion inicial desde cero, ver
[`README.md`](./README.md) §"Arranque rapido".

---

## 1. Arrancar el servidor (desarrollo)

Desde `c:\dev\telycoposio-tickets`:

```powershell
.\.venv\Scripts\uvicorn.exe app.main:app --reload
```

- Recarga automatica al editar codigo (`--reload`).
- Logs salen por la terminal donde lo arrancas.
- Si tienes `WORKER_ENABLED=true` y/o `GSHEETS_ENABLED=true` en `.env`,
  veras al arrancar: `scheduler arrancado con jobs=[...]`.

**URL de la interfaz**: <http://localhost:8000/>

- `/` redirige a `/login` si no hay sesion, o a `/tickets` si la hay.
- `/health` devuelve `{"status":"ok"}` (healthcheck del contenedor).

Parar el servidor: `Ctrl+C` en la terminal.

---

## 2. Usuarios y contraseñas

### Listar usuarios existentes

```powershell
.\.venv\Scripts\python.exe -c "import sqlite3; c = sqlite3.connect('data/app.db'); c.row_factory = sqlite3.Row; [print(dict(r)) for r in c.execute('SELECT username, display_name, role FROM users')]"
```

### Crear un usuario nuevo

```powershell
.\.venv\Scripts\python.exe scripts/add_user.py --username <user> --display-name "<Nombre>" --role admin
```

- Roles validos: `admin` o `user`.
- Pide la contrasena por stdin dos veces (sin eco). Minimo 8 caracteres.

### Recuperar / resetear contrasena

**No hay forma de recuperar la contrasena original** (esta hasheada con
bcrypt y no es reversible). La via es borrar el usuario y recrearlo:

```powershell
# 1. Borra el usuario (cambia 'alfredo' por el tuyo)
.\.venv\Scripts\python.exe -c "import sqlite3; c = sqlite3.connect('data/app.db'); c.execute('DELETE FROM users WHERE username = ?', ('alfredo',)); c.commit(); print('borrado')"

# 2. Recrealo con la contrasena nueva
.\.venv\Scripts\python.exe scripts/add_user.py --username alfredo --display-name "Alfredo" --role admin
```

No hace falta reiniciar uvicorn: la BD se lee fresca en cada login.

---

## 3. Tests

Suite por defecto (sin red, sin coste):

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests contra APIs reales (Anthropic, IMAP/SMTP — coste y latencia):

```powershell
.\.venv\Scripts\python.exe -m pytest -m real_api
```

---

## 4. Datos de demo

Para ver la UI con datos sin esperar a que entren emails reales:

```powershell
.\.venv\Scripts\python.exe scripts/seed_demo_tickets.py            # idempotente
.\.venv\Scripts\python.exe scripts/seed_demo_tickets.py --reset    # recrea desde cero
```

Los tickets demo tienen `from_email = 'demo@ejemplo.com'`: se purgan
limpiamente con un solo `DELETE WHERE`.

---

## 5. Smoke tests manuales contra servicios reales

Cada uno toca cuentas reales (cuesta dinero o produce side-effects).
Util cuando algo va mal y quieres aislar la capa.

### Email (Paso 7)

```powershell
.\.venv\Scripts\python.exe scripts/email_smoke.py --list                       # lista UIDs sin procesar
.\.venv\Scripts\python.exe scripts/email_smoke.py --get <UID>                  # parsea un email
.\.venv\Scripts\python.exe scripts/email_smoke.py --send-test <to>             # envia un mensaje de prueba
.\.venv\Scripts\python.exe scripts/email_smoke.py --mark-all-existing-processed --yes  # bootstrap pre-Paso 8
```

### Clasificador IA (Paso 6)

```powershell
.\.venv\Scripts\python.exe scripts/classify_text.py --subject "Asunto" --text "Cuerpo del mensaje"
```

Coste por llamada < 0.001 EUR.

### Google Sheets (Paso 9)

```powershell
.\.venv\Scripts\python.exe scripts/sheets_smoke.py                 # sube tickets pendientes
.\.venv\Scripts\python.exe scripts/sheets_smoke.py --max 10        # limita el batch
.\.venv\Scripts\python.exe scripts/sheets_smoke.py --reset-sync    # fuerza re-sync masivo (confirma con 'si')
```

Para que el sync corra solo cada 5 minutos al arrancar uvicorn, basta
`GSHEETS_ENABLED=true` en `.env`. Para off, `false`.

---

## 6. Flags de activacion en `.env`

Dos worker-flags **off por defecto** para que arranques en dev no
toquen Gmail/Sheets sin querer:

| Variable           | Efecto si `true`                                              |
|--------------------|---------------------------------------------------------------|
| `WORKER_ENABLED`   | Arranca el poller de email cada `EMAIL_POLL_INTERVAL_SECONDS` |
| `GSHEETS_ENABLED`  | Arranca el sync a Sheets cada `GSHEETS_SYNC_INTERVAL_SECONDS` |

Si `GSHEETS_ENABLED=true`, son obligatorios `GSHEETS_SPREADSHEET_ID` y
`GSHEETS_CREDENTIALS_PATH` (la app no arranca si faltan).

---

## 7. BD: localizacion y reset

- Fichero: `data/app.db` (SQLite, no versionado).
- Recrear desde cero (BORRA TODO):
  ```powershell
  Remove-Item data/app.db
  .\.venv\Scripts\python.exe scripts/init_db.py
  ```
  Despues hay que volver a crear usuarios con `add_user.py`.

---

## 8. Despliegue (produccion)

Solo cuando toque el Paso 11. Resumen:

```bash
docker compose build
docker compose up -d
docker compose ps          # esperar healthcheck "healthy"
docker compose logs -f     # ver logs en vivo
```

Volumenes montados: `./data` (BD) y `./secrets` (credenciales).
