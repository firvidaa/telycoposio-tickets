# SETUP — Arranque desde cero en un equipo nuevo

Guia paso a paso para dejar el proyecto operativo en una maquina
limpia (Windows + PowerShell). Si vas a continuar el desarrollo,
despues de completar este SETUP lee [`HANDOFF.md`](./HANDOFF.md) para
saber donde estabamos.

Documentos hermanos:

- [`HANDOFF.md`](./HANDOFF.md) — estado del proyecto y siguiente paso.
- [`INICIO.md`](./INICIO.md) — cheatsheet de comandos del dia a dia.
- [`CLAUDE.md`](./CLAUDE.md) — contexto operativo permanente.
- [`README.md`](./README.md) — descripcion general y stack.

---

## 0. Requisitos previos (instalar en el sistema)

### Obligatorios

| Software | Version | Notas |
|---|---|---|
| **Git** | 2.40+ | <https://git-scm.com/download/win>. Acepta los defaults. |
| **Python** | 3.11.x **minimo** | <https://www.python.org/downloads/>. Marca "Add python to PATH" durante la instalacion. Probado con 3.14 y 3.11. Si introduces sintaxis 3.12+ habra que actualizar el Dockerfile (`python:3.11-slim`). |

Verifica tras instalar:

```powershell
git --version       # esperado: git version 2.4x.x
python --version    # esperado: Python 3.11.x o superior
```

### Recomendados

| Software | Para que |
|---|---|
| **VSCode** o **Cursor** | Editor con soporte de Python y Git. <https://code.visualstudio.com/> |
| **Claude Code** (extension de VSCode/Cursor) | Continuar el desarrollo asistido. |

### Opcional (solo si vas a tocar despliegue prod, Paso 11)

| Software | Para que |
|---|---|
| **Docker Desktop** | Construir y arrancar el contenedor de produccion. <https://www.docker.com/products/docker-desktop/>. NO se usa para desarrollo diario. |

---

## 1. Llevar a mano del equipo origen

Estos ficheros NO estan en el repo (gitignored) y hay que pasarlos
por medio seguro (USB cifrado, gestor de passwords). **Nunca** por
chat, email o cloud sin cifrar.

| Path | Que es | Necesario? |
|---|---|---|
| `.env` | Variables de entorno reales (claves, App Password Gmail) | **Si**. Sin el la app no arranca. |
| `secrets/gsheets_service_account.json` | Credenciales del service account de Google para Sheets | **Si** si vas a usar el sync a Sheets. |
| `data/app.db` | BD SQLite con tickets y usuarios actuales | **Opcional**. Si la copias preservas historial; si no, se recrea vacia. |

Si prefieres empezar con BD limpia: deja `data/app.db` fuera (lo
recreas en el paso 5).

---

## 2. Clonar el repo

```powershell
git clone https://github.com/firvidaa/telycoposio-tickets.git
cd telycoposio-tickets
```

---

## 3. Copiar los ficheros de credenciales

Coloca a mano los ficheros del paso 1 en su sitio:

```
telycoposio-tickets/
├── .env                                    <- aqui
├── secrets/
│   └── gsheets_service_account.json        <- aqui
└── data/
    └── app.db                              <- aqui (opcional)
```

Comprobacion rapida desde PowerShell:

```powershell
Test-Path .env
Test-Path secrets/gsheets_service_account.json
Test-Path data/app.db
```

Los tres deberian devolver `True` (o `True/True/False` si decides no
copiar la BD).

---

## 4. Crear venv e instalar dependencias

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

> Si PowerShell bloquea el `Activate.ps1` con un error de scripts,
> ejecuta una vez (como Administrador no es necesario):
> ```powershell
> Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
> ```
> Y vuelve a intentar el `Activate.ps1`.

El `pip install -e ".[dev]"` instala el proyecto en modo editable + las
dependencias de desarrollo (`pytest`, `ruff`, `mypy`).

---

## 5. Inicializar la BD (solo si NO copiaste `data/app.db`)

```powershell
.\.venv\Scripts\python.exe scripts/init_db.py
.\.venv\Scripts\python.exe scripts/add_user.py --username alfredo --display-name "Alfredo" --role admin
```

Te pedira la contrasena del usuario (sin eco, dos veces). Minimo 8
caracteres.

---

## 6. Verificar tests verdes

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Resultado esperado:

```
283 passed, 2 deselected in ~16s
```

Si algun test falla, **para aqui** y revisalo antes de seguir. Lo
mas probable: falta una dependencia o el `.env` esta mal formado.

---

## 7. Bootstrap del worker (CRITICO antes del primer arranque)

> **Solo necesario si `WORKER_ENABLED=true` en `.env` Y la cuenta de
> soporte (`EMAIL_ADDRESS`) tiene emails historicos en el INBOX.**

Si no haces esto, el poller convertira todos los emails del INBOX en
tickets y mandara auto-respuestas a remitentes que no las esperan
(potencial bucle con auto-responders). Ver [INICIO.md §9.1](./INICIO.md).

Antes de arrancar uvicorn por primera vez:

```powershell
# Pone WORKER_ENABLED=false en .env como red de seguridad
notepad .env

# Marca todos los emails actuales del INBOX como ya procesados
.\.venv\Scripts\python.exe scripts/email_smoke.py --mark-all-existing-processed --yes

# Ahora si: pon WORKER_ENABLED=true en .env
notepad .env
```

Si la cuenta IMAP es nueva o el INBOX esta vacio, este paso no es
necesario. Pero NUNCA esta de mas (es idempotente).

---

## 8. Arrancar el servidor

```powershell
.\.venv\Scripts\uvicorn.exe app.main:app --reload
```

Logs esperados al arrancar:

```
INFO:     Started server process [...]
INFO:     Application startup complete.
INFO:     scheduler arrancado con jobs=['email_poller@60s', 'sheets_sync@300s']
INFO:     Uvicorn running on http://0.0.0.0:8000
```

> Si solo ves un job (`email_poller@60s` o `sheets_sync@300s`), uno de
> los dos flags esta en `false` en `.env`. Es lo esperado en dev local.

---

## 9. Verificar en el navegador

Abre <http://localhost:8000/>. Te redirige a `/login`.

Entra con tu usuario + contrasena del paso 5.

Si todo OK, ves el listado de tickets en `/tickets`.

---

## 10. Siguientes pasos

Setup terminado. Ahora:

- Para empezar a desarrollar -> [HANDOFF.md](./HANDOFF.md) §6
  (siguiente paso: Paso 11).
- Para comandos del dia a dia -> [INICIO.md](./INICIO.md).
- Para entender una decision o convencion -> [CLAUDE.md](./CLAUDE.md).
- Para retomar con Claude Code una sesion asistida:
  > "Lee HANDOFF.md y dime donde estamos."

---

## Troubleshooting

### `pip install` falla con error de compilacion en bcrypt / cryptography

En Windows, algunas dependencias necesitan Visual C++ Build Tools.
Instalalas desde:
<https://visualstudio.microsoft.com/visual-cpp-build-tools/>

### `uvicorn` no arranca: "ValidationError: APP_SECRET_KEY..."

Falta o esta mal `APP_SECRET_KEY` en `.env`. Genera una nueva:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Y pegala en `.env` como `APP_SECRET_KEY=<valor>`.

### `uvicorn` arranca pero `GSHEETS_ENABLED=true` y peta

El validador de `Settings` exige que `GSHEETS_SPREADSHEET_ID` y
`GSHEETS_CREDENTIALS_PATH` esten rellenos. Revisa el `.env` y el
fichero referenciado por `GSHEETS_CREDENTIALS_PATH`.

### El poller manda auto-respuestas a emails antiguos

No corriste el bootstrap del paso 7. Para:

1. Para uvicorn (Ctrl+C).
2. `WORKER_ENABLED=false` en `.env`.
3. Borra los tickets erroneos:
   ```powershell
   .\.venv\Scripts\python.exe -c "import sqlite3; c = sqlite3.connect('data/app.db'); c.execute('DELETE FROM ticket_replies'); c.execute('DELETE FROM tickets'); c.commit()"
   ```
4. Borra las filas en la hoja de Sheets a mano.
5. Corre el bootstrap del paso 7 ahora.
6. `WORKER_ENABLED=true` y arranca uvicorn.
