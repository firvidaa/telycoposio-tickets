# Sistema de Tickets Telycoposio — Especificación del Proyecto

> **Versión:** 1.1
> **Fecha:** 2026-05-05
> **Estado:** Diseño inicial

### Cambios respecto a v1.0

- **§2.4 / §4.1** — Se añade campo `status` al modelo de tickets con cuatro valores (`NEW`, `IN_PROGRESS`, `WAITING`, `CLOSED`). Antes estaba listado como fuera de alcance; se incorpora desde el MVP por ser imprescindible para el flujo de trabajo diario.
- **§4.1** — `category` pasa a ser opcional (puede ser `NULL` cuando la IA falla la clasificación).
- **§4.1** — Se añade campo booleano `needs_review`, que se pone a `true` cuando la IA falla **o** cuando `category_confidence < 0.7`. Permite un único filtro para "tickets que un humano debe revisar" sin contaminar la categoría real con un valor sintético tipo `PENDIENTE_REVISION`.

---

## 1. Resumen ejecutivo

Plataforma interna de gestión de tickets de atención al cliente para **Telycoposio**, una pequeña empresa de 3 personas dedicada a la venta de material informático y de telecomunicaciones, así como soporte técnico (averías de telefonía, VoIP, red, equipos) y gestiones administrativas.

**Objetivo principal:** centralizar todas las solicitudes entrantes (email, WhatsApp y, en una fase posterior, buzón de voz de la centralita) en un único sistema de tickets numerados, organizados y categorizados automáticamente mediante IA. Eliminar las llamadas y mensajes directos al móvil personal de los empleados.

---

## 2. Alcance funcional

### 2.1. Lo que debe hacer (MVP — Fase 1)

- Recibir emails entrantes en la cuenta `soportetelycoposio@gmail.com` y crear automáticamente un ticket por cada uno.
- Asignar a cada ticket un **número correlativo único** (formato `TLY-2026-0001`).
- **Categorizar automáticamente** el ticket usando la API de Anthropic (Claude Haiku) en una de tres categorías:
  - `ADMINISTRATIVO` (facturas, presupuestos, contratos, gestiones)
  - `COMERCIAL` (consultas de productos, ventas, presupuestos de material)
  - `SOPORTE` (incidencias, averías, problemas técnicos)
- Guardar todos los tickets en **Google Sheets** (con SQLite local como caché).
- Mostrar una **interfaz web local minimalista** accesible desde la oficina (y vía VPN para acceso remoto) con:
  - Login por usuario (3 cuentas: una por empleado).
  - Listado filtrable de tickets.
  - Vista de detalle de cada ticket.
  - Posibilidad de responder al cliente desde la propia plataforma.
  - Posibilidad de cambiar manualmente la categoría si la IA se equivoca.
- **Enviar email automático de confirmación** al cliente al crear el ticket:
  > *"Hola, hemos recibido tu solicitud. Tu número de ticket es #TLY-2026-XXXX. Te responderemos lo antes posible. — Equipo Telycoposio"*
- **Notificar internamente** a `soporte@telycoposio.com` cuando entre un nuevo ticket.

### 2.2. Lo que se añadirá (Fase 2)

- Integración con **WhatsApp Cloud API** (oficial de Meta) — recepción y respuesta de mensajes de WhatsApp como tickets.

### 2.3. Lo que se añadirá (Fase 3)

- Integración con **API REST de Aire Networks (Grupo Aire)** para:
  - Descargar audios del buzón de voz.
  - Transcribirlos automáticamente (Whisper o similar).
  - Crear tickets a partir de las transcripciones.

### 2.4. Fuera de alcance (por ahora, anotado para el futuro)

- Asignación de tickets a agentes específicos.
- Historial de respuestas y conversaciones.
- SLAs y alertas por tiempo de respuesta.
- Dashboards de métricas y reportes.
- Plantillas de respuesta predefinidas.
- Subcategorías.
- Prioridades.

> **Nota v1.1:** los estados de ticket (`NEW` / `IN_PROGRESS` / `WAITING` / `CLOSED`) sí entran en el MVP — ver §4.1.

---

## 3. Stack técnico

| Componente | Tecnología elegida | Motivo |
|---|---|---|
| Lenguaje | Python 3.11+ | Sencillo, ecosistema enorme, ideal para automatización |
| Framework web | FastAPI | Moderno, rápido, documentación automática |
| Plantillas HTML | Jinja2 + Tailwind CSS (vía CDN) | Frontend simple sin build complicado |
| Base de datos primaria | Google Sheets (vía API) | Visibilidad humana directa, requisito del cliente |
| Caché / DB local | SQLite | Resiliente si Google Sheets falla, búsquedas rápidas |
| Email entrante/saliente | Gmail API (OAuth 2.0) | Oficial, robusto, mejor que IMAP/SMTP |
| Categorización IA | Anthropic API — Claude Haiku 4.5 | Modelo barato y rápido, ideal para clasificación |
| Autenticación interna | Sesiones con cookies + bcrypt para passwords | Sencillo y seguro para 3 usuarios |
| Despliegue | Docker + Docker Compose en servidor de oficina | Portable y reproducible |
| Sistema operativo del servidor | Linux (Ubuntu Server 24.04 LTS recomendado) | Estable, gratuito, ideal para servicios 24/7 |
| Acceso remoto | VPN (a configurar aparte por el usuario) | Seguridad sin exponer servicios a internet |

### 3.1. Servicios externos necesarios

| Servicio | Coste estimado mensual | Proveedor |
|---|---|---|
| Cuenta Gmail dedicada | 0 € | Google |
| API de Anthropic (Claude) | < 1 € (estimado para 200 tickets/mes con Haiku) | Anthropic |
| WhatsApp Cloud API (Fase 2) | 0 € (servicio: <1.000 conv./mes gratis e ilimitadas desde nov-2024) | Meta |
| API Aire Networks (Fase 3) | Incluido en el servicio actual de centralita | Grupo Aire |

---

## 4. Modelo de datos

### 4.1. Tabla `tickets` (Google Sheets + SQLite)

| Campo | Tipo | Descripción |
|---|---|---|
| `id` | TEXT PK | Identificador único, formato `TLY-2026-0001` |
| `created_at` | DATETIME | Fecha/hora de creación |
| `channel` | TEXT | `email` / `whatsapp` / `voicemail` |
| `from_name` | TEXT | Nombre del remitente (si se conoce) |
| `from_email` | TEXT | Email del remitente (si aplica) |
| `from_phone` | TEXT | Teléfono del remitente (si aplica) |
| `subject` | TEXT | Asunto / título del ticket |
| `body` | TEXT | Contenido completo del mensaje original |
| `status` | TEXT | `NEW` / `IN_PROGRESS` / `WAITING` / `CLOSED`. Por defecto `NEW` al crear el ticket. |
| `category` | TEXT NULL | `ADMINISTRATIVO` / `COMERCIAL` / `SOPORTE`. Puede ser `NULL` si la IA falla la clasificación. |
| `category_confidence` | REAL | Confianza de la IA (0.0–1.0). `NULL` si la IA falla. |
| `category_reasoning` | TEXT | Razonamiento breve de la IA. `NULL` si la IA falla. |
| `category_manual_override` | BOOLEAN | `True` si un humano cambió la categoría. |
| `needs_review` | BOOLEAN | `True` si la IA falló (`category IS NULL`) o si `category_confidence < 0.7`. Permite filtrar tickets que requieren revisión humana. |
| `raw_message_id` | TEXT | ID original del mensaje (Gmail message-id, etc.) para evitar duplicados |
| `attachments` | TEXT (JSON) | Lista de adjuntos: `[{"name":"factura.pdf","url":"..."}]` |
| `client_notified_at` | DATETIME | Cuándo se envió el email de confirmación |
| `last_updated_at` | DATETIME | Última modificación |

### 4.2. Tabla `users` (solo SQLite local)

| Campo | Tipo | Descripción |
|---|---|---|
| `id` | INTEGER PK | |
| `username` | TEXT UNIQUE | Login |
| `password_hash` | TEXT | bcrypt |
| `display_name` | TEXT | Nombre visible |
| `email` | TEXT | Para notificaciones internas |
| `created_at` | DATETIME | |

### 4.3. Tabla `sessions` (solo SQLite local)

Sesiones activas para login web.

---

## 5. Flujos principales

### 5.1. Entrada de email → Ticket

```
[Gmail INBOX]
     ↓ (polling cada 60s vía Gmail API)
[Worker Python]
     ↓ filtra emails no procesados (label `procesado` no presente)
     ↓ extrae remitente, asunto, cuerpo, adjuntos
     ↓ llama a Anthropic API → categoría + confianza
     ↓ genera ID único (TLY-AAAA-NNNN)
[SQLite] ← inserta ticket
[Google Sheets] ← inserta ticket (vía API)
     ↓
[Gmail API] → envía confirmación al cliente
[Gmail API] → envía notificación interna a soporte@telycoposio.com
[Gmail API] → marca el email original con label `procesado`
```

### 5.2. Login y consulta web

```
[Navegador] → http://servidor-oficina:8000/login
     ↓ credenciales
[FastAPI] → verifica con bcrypt contra SQLite users
     ↓ crea sesión
[FastAPI] → renderiza /tickets con listado filtrable
     ↓
[Usuario] → click en ticket
[FastAPI] → /tickets/TLY-2026-0001 → vista detalle
     ↓
[Usuario] → escribe respuesta y envía
[Gmail API] → envía email al cliente
[SQLite + Sheets] → registra que se respondió
```

---

## 6. Estructura de carpetas del proyecto

```
telycoposio-tickets/
├── README.md                    # Cómo arrancar el proyecto
├── SPEC.md                      # Este documento
├── .env.example                 # Variables de entorno (plantilla)
├── .gitignore
├── docker-compose.yml           # Orquestación de contenedores
├── Dockerfile
├── pyproject.toml               # Dependencias Python (uv/poetry)
├── app/
│   ├── __init__.py
│   ├── main.py                  # Punto de entrada FastAPI
│   ├── config.py                # Carga de configuración desde .env
│   ├── db/
│   │   ├── __init__.py
│   │   ├── sqlite.py            # Conexión y migraciones SQLite
│   │   └── sheets.py            # Cliente Google Sheets
│   ├── models/
│   │   ├── __init__.py
│   │   ├── ticket.py
│   │   └── user.py
│   ├── services/
│   │   ├── __init__.py
│   │   ├── gmail.py             # Lectura/envío vía Gmail API
│   │   ├── classifier.py        # Llamada a Anthropic API
│   │   ├── ticket_service.py    # Lógica de negocio de tickets
│   │   └── auth.py              # Login, sesiones, bcrypt
│   ├── workers/
│   │   ├── __init__.py
│   │   └── email_poller.py      # Tarea en background que lee Gmail
│   ├── web/
│   │   ├── __init__.py
│   │   ├── routes.py            # Rutas FastAPI
│   │   ├── templates/           # HTML (Jinja2)
│   │   │   ├── base.html
│   │   │   ├── login.html
│   │   │   ├── tickets_list.html
│   │   │   └── ticket_detail.html
│   │   └── static/              # CSS, JS si hace falta
│   └── utils/
│       ├── __init__.py
│       └── logging.py
├── scripts/
│   ├── init_db.py               # Inicializa SQLite y crea usuarios
│   ├── add_user.py              # CLI para añadir usuario
│   └── test_apis.py             # Verifica que todas las APIs responden
├── tests/
│   └── test_classifier.py       # Tests básicos
└── data/                        # No versionar — montaje persistente
    └── app.db                   # SQLite
```

---

## 7. Variables de entorno (`.env`)

```ini
# === Aplicación ===
APP_ENV=production
APP_SECRET_KEY=<generar con: python -c "import secrets; print(secrets.token_urlsafe(32))">
APP_HOST=0.0.0.0
APP_PORT=8000

# === Anthropic ===
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-haiku-4-5-20251001

# === Gmail (OAuth) ===
GMAIL_ADDRESS=soportetelycoposio@gmail.com
GMAIL_CREDENTIALS_PATH=/app/secrets/gmail_credentials.json
GMAIL_TOKEN_PATH=/app/secrets/gmail_token.json
GMAIL_POLL_INTERVAL_SECONDS=60

# === Notificación interna ===
INTERNAL_NOTIFICATION_EMAIL=soporte@telycoposio.com

# === Google Sheets ===
GSHEETS_SPREADSHEET_ID=<id de la hoja de cálculo>
GSHEETS_CREDENTIALS_PATH=/app/secrets/gsheets_service_account.json

# === Base de datos local ===
SQLITE_PATH=/app/data/app.db

# === Configuración de tickets ===
TICKET_PREFIX=TLY
TICKET_YEAR_AUTO=true
```

---

## 8. Cumplimiento RGPD (España/UE)

- **Base legal:** ejecución de contrato / interés legítimo (atención a clientes existentes y solicitudes propias).
- **Datos almacenados:** identificadores mínimos necesarios (nombre, email/teléfono, contenido del mensaje).
- **Conservación:** revisar política. Propuesta inicial: 24 meses desde el cierre del ticket, después anonimización.
- **Derechos ARCO:** debe existir un procedimiento para que un cliente pueda solicitar acceso, rectificación o borrado de sus datos.
- **Cifrado:** las credenciales y tokens se almacenan en archivos con permisos `600`. Las contraseñas de usuarios con `bcrypt`.
- **Acceso:** restringido por VPN + login.
- **Registro de actividades de tratamiento:** documentar en el RAT de la empresa.
- **Aviso de privacidad:** incluir nota en el email de confirmación al cliente: *"Tus datos serán tratados por Telycoposio para gestionar tu solicitud. Más información: [enlace política]."*

---

## 9. Seguridad

- Todas las claves API y credenciales **fuera** del código fuente (en `.env`, no commitear).
- El servidor solo escucha en la red local; acceso externo solo vía VPN.
- Backup diario automatizado de SQLite y export de Google Sheets.
- Logs sin contenido sensible (no logear cuerpo completo de emails ni passwords).
- Rate limiting básico en el login (max 5 intentos por minuto).

---

## 10. Plan de fases con entregables

### Fase 1 — MVP Email + Web (estimado 1-2 semanas con Claude Code)

- [ ] Setup del proyecto (estructura, dependencias, Docker).
- [ ] Cuenta Gmail creada y configurada.
- [ ] Hoja de Google Sheets creada con la estructura de columnas.
- [ ] Service account de Google con acceso a la hoja.
- [ ] Cuenta de Anthropic con API key.
- [ ] Worker que lee Gmail cada 60s y crea tickets.
- [ ] Categorización con Claude Haiku funcionando.
- [ ] Web local con login, listado y detalle de tickets.
- [ ] Respuesta al cliente desde la web.
- [ ] Email automático de confirmación al cliente.
- [ ] Notificación interna a `soporte@telycoposio.com`.
- [ ] Despliegue en servidor de oficina con Docker Compose.
- [ ] Backup automatizado.

### Fase 2 — WhatsApp Cloud API

- [ ] Verificación de empresa en Meta Business.
- [ ] Configuración de WhatsApp Coexistence (mantener app + API en mismo número).
- [ ] Endpoint webhook en FastAPI para recibir mensajes.
- [ ] Plantillas aprobadas por Meta para confirmaciones y avisos de estado.
- [ ] UI para responder por WhatsApp desde la plataforma.

### Fase 3 — Buzón de voz Aire Networks

- [ ] Solicitar credenciales API a Grupo Aire.
- [ ] Endpoint que descarga audios nuevos.
- [ ] Transcripción con Whisper local o API (decidir según coste).
- [ ] Creación de tickets desde transcripciones con marca "AUDIO_BUZON".
- [ ] Reproductor de audio embebido en la vista detalle del ticket.

---

## 11. Riesgos y mitigaciones

| Riesgo | Probabilidad | Impacto | Mitigación |
|---|---|---|---|
| Gmail API limita cuota | Baja | Medio | Polling cada 60s en lugar de cada 5s; usar webhooks push de Gmail más adelante |
| Categorización IA errónea | Media | Bajo | Permitir override manual; revisar tickets con `confidence < 0.7` |
| Servidor de oficina se apaga | Media | Alto | Backup diario + UPS recomendado |
| Pérdida de credenciales OAuth | Baja | Alto | Documentar reautenticación; alertas si tokens fallan |
| Spam recibido como ticket | Alta | Bajo | Filtros previos en Gmail (filter de spam, lista negra) |
| Volumen crece > 50/semana | Media | Bajo | Arquitectura permite escalar; revisar a los 6 meses |

---

## 12. Glosario

- **Ticket:** unidad de solicitud o incidencia recibida por cualquier canal.
- **MVP:** Mínimo Producto Viable — la versión más pequeña útil.
- **OAuth 2.0:** protocolo estándar para autenticarse contra APIs de Google.
- **Webhook:** URL pública que recibe notificaciones de eventos (no usado en Fase 1, sí en Fase 2 para WhatsApp).
- **VPN:** Red Privada Virtual; permite el acceso seguro al servidor desde fuera de la oficina.
- **RGPD:** Reglamento General de Protección de Datos (UE).
