# Checklist previo — Antes de pegar el prompt en Claude Code

Antes de arrancar el proyecto, hay tareas que tienes que hacer **tú a mano**
(crear cuentas, conseguir API keys, etc.). Claude Code no puede hacerlas por ti
porque requieren tu identidad, tu tarjeta o decisiones tuyas.

Sigue esta lista en orden. No hace falta hacerlo todo de golpe — puedes
empezar con lo básico y completar el resto cuando Claude Code te lo pida.

---

## ✅ Lo MÍNIMO para arrancar el Paso 1 (estructura del proyecto)

Solo necesitas tener instalado en tu PC:

- [ ] **Python 3.11 o superior**
  - Web: https://www.python.org/downloads/
  - Verifica con: `python --version`

- [ ] **VS Code**
  - Web: https://code.visualstudio.com/

- [ ] **Claude Code** (ya lo tienes instalado, dijiste)
  - Verifica con: `claude --version`

- [ ] **Git** (para control de versiones, muy recomendable)
  - Web: https://git-scm.com/

Con eso ya puedes pegar el prompt y empezar.

---

## ✅ Cuando Claude Code te lo pida — Cuentas y credenciales

### 1. Cuenta Gmail dedicada

- [ ] Crea una cuenta nueva: `soportetelycoposio@gmail.com` (o el nombre que
      decidas).
- [ ] Activa la **verificación en dos pasos** (obligatorio para usar API).
- [ ] Ve a https://console.cloud.google.com/ y crea un proyecto nuevo
      llamado `telycoposio-tickets`.
- [ ] En ese proyecto, activa la **Gmail API** y la **Google Sheets API**.
- [ ] Crea credenciales OAuth 2.0 tipo "Desktop app" → descargas un JSON.
      Lo guardas como `gmail_credentials.json`.
- [ ] **Claude Code te guiará en este paso cuando llegue el momento.**

### 2. Cuenta de Anthropic con API key

- [ ] Date de alta en https://console.anthropic.com/
- [ ] Añade método de pago (la API es de pago por uso).
- [ ] Pon un **límite de gasto mensual** bajo para empezar (ej: 10 €/mes).
      Para tu volumen real (200 tickets/mes con Haiku) gastarás céntimos,
      pero el límite te protege.
- [ ] Genera una API key y guárdala en sitio seguro
      (será de la forma `sk-ant-...`).

### 3. Hoja de Google Sheets

- [ ] Crea una nueva hoja en https://sheets.google.com/
- [ ] Llámala `Tickets Telycoposio - 2026`.
- [ ] Apunta el ID de la hoja (es la parte larga de la URL,
      `https://docs.google.com/spreadsheets/d/AQUI_VA_EL_ID/edit`).
- [ ] Crea un **Service Account** en Google Cloud (Claude Code te
      explicará cómo) y comparte la hoja con su email para que el
      programa pueda escribir en ella.

### 4. Servidor donde se va a desplegar

- [ ] Decide qué equipo va a hacer de servidor en la oficina.
- [ ] Si tiene Windows, considera instalar **WSL2 con Ubuntu** o
      **Ubuntu Server** directamente.
- [ ] Necesita estar **encendido 24/7** y conectado a la red local con
      IP fija (o reserva DHCP).
- [ ] Recomendado: un SAI / UPS pequeño para que no se apague con
      cortes de luz breves.
- [ ] Anota la IP local del servidor (tipo `192.168.1.50`).

### 5. VPN para acceso remoto

- [ ] Para acceder desde fuera de la oficina necesitas VPN.
      Opciones recomendadas (gratis y fáciles):
      - **Tailscale** (https://tailscale.com/) — la más sencilla.
      - **WireGuard** — clásica y robusta, algo más manual.
- [ ] No es bloqueante para empezar a desarrollar; lo configuras al
      final, antes de poner el sistema en uso real.

---

## ✅ Pendientes para Fases 2 y 3 (NO bloqueantes ahora)

### Fase 2 — WhatsApp Cloud API

- [ ] Cuenta de **Meta Business**: https://business.facebook.com/
- [ ] Verificación de la empresa (proceso de Meta — gratis pero lleva días).
- [ ] Crear app en https://developers.facebook.com/ con producto
      "WhatsApp".
- [ ] Configurar **Coexistence** para mantener la app del móvil Y la API
      en el mismo número.
- [ ] Diseñar y solicitar aprobación de **plantillas** de mensajes
      (Meta debe aprobarlas antes de poder usarlas).

### Fase 3 — Buzón de voz Aire Networks

- [ ] Abre un ticket con el soporte de Grupo Aire pidiendo:
      - Documentación de la API REST de la Oficina Virtual de Voz.
      - Credenciales de acceso a la API.
      - Confirmación de que tu plan actual incluye acceso a la API.
- [ ] Decide proveedor de transcripción:
      - OpenAI Whisper (API) — barato, buena calidad.
      - Whisper local (gratis pero requiere PC con GPU).
      - Google Speech-to-Text — alternativa.

---

## ✅ Documentos legales pendientes (RGPD)

- [ ] Revisar y, si hace falta, actualizar la **política de privacidad**
      de Telycoposio para mencionar el tratamiento de datos en este
      sistema de tickets.
- [ ] Añadir el sistema al **Registro de Actividades de Tratamiento (RAT)**.
- [ ] Definir política de retención (propuesta inicial: 24 meses).
- [ ] Procedimiento interno para responder a solicitudes ARCO de clientes.

Si no sois muchos y no tenéis DPO, con leerse la guía de la AEPD para
PYMES tendréis suficiente: https://www.aepd.es/

---

## 🚦 Resumen: ¿Por dónde empiezo HOY?

1. Verifica que tienes Python 3.11+, VS Code, Claude Code y Git instalados.
2. Crea una carpeta vacía en tu disco para el proyecto.
3. Copia dentro `SPEC.md` y `PROMPT_BASE.md`.
4. Abre VS Code en esa carpeta, abre la terminal y lanza `claude`.
5. Pega el prompt completo de `PROMPT_BASE.md`.
6. Sigue lo que te vaya pidiendo Claude Code paso a paso.

El resto (cuentas Google, Anthropic, etc.) lo irás haciendo según Claude
te lo pida. Así no te abrumas creando 5 cuentas a la vez sin saber para
qué sirve cada una.
