# Prompt base para Claude Code — Sistema de Tickets Telycoposio

Este es el prompt que pegarás en Claude Code la primera vez que arranques el proyecto. Cópialo entero, desde la línea siguiente al comienzo de la cita hasta el final.

---

## 📋 PROMPT (copiar desde aquí ↓)

```
Hola Claude. Vamos a construir juntos un sistema de tickets para una pequeña
empresa española llamada Telycoposio. Antes de escribir una sola línea de
código, lee TODO el archivo SPEC.md que está en la raíz de este proyecto.
Esa es la fuente de la verdad: si algo no está ahí, pregúntame antes de
inventar.

== Contexto del usuario que tienes que recordar ==

Soy un programador novato. Tengo nociones muy básicas. No asumas que conozco
ningún concepto técnico avanzado. Cuando vayas a hacer algo, sigue siempre
este patrón:

  1. Explica brevemente y en español qué vas a hacer y por qué.
  2. Si vas a instalar algo o tocar configuración del sistema, pídeme
     confirmación antes.
  3. Cuando termines un bloque de trabajo, dime cómo verificar que
     funciona (qué comando ejecutar, qué debería ver yo).
  4. Si te encuentras una decisión técnica con varias opciones, lístalas
     con sus pros y contras y deja que yo elija. No decidas tú solo cosas
     importantes.

Trabajo en Windows, pero el servidor donde correrá todo será Ubuntu Server.
Para desarrollo, vamos a usar Docker desde el principio para que lo que
funcione en mi máquina funcione idéntico en el servidor.

== Stack técnico (no cambies esto sin avisarme) ==

- Python 3.11+
- FastAPI (web framework)
- Jinja2 + Tailwind CSS vía CDN (frontend)
- SQLite (caché local) + Google Sheets (DB primaria)
- Gmail API para email entrante y saliente
- API de Anthropic con modelo claude-haiku-4-5-20251001 para categorización
- bcrypt para passwords
- Docker + Docker Compose para empaquetado
- uv como gestor de dependencias Python (más rápido que pip)

== Categorías de tickets ==

Solo tres, sin subcategorías:

  - ADMINISTRATIVO  (facturas, presupuestos, contratos, gestiones varias)
  - COMERCIAL       (consultas de productos, ventas, presupuestos de material
                     informático y de telecomunicaciones)
  - SOPORTE         (incidencias, averías de telefonía, VoIP, red, equipos
                     informáticos o de telecomunicaciones)

Formato de ID de ticket: TLY-AAAA-NNNN  (ej: TLY-2026-0001), correlativo
por año.

== Plan de trabajo en pasos ==

Vamos en orden, NO te saltes pasos. Cuando termines uno, párate y espera
mi confirmación antes de seguir. Estos son los pasos de la Fase 1:

  Paso 1.  Estructura del proyecto y entorno básico (carpetas, pyproject,
           .gitignore, .env.example, README inicial).
  Paso 2.  Docker y Docker Compose básicos para que pueda lanzar el
           contenedor con un solo comando.
  Paso 3.  Modelo de datos: schemas Pydantic + tablas SQLite con
           migraciones simples.
  Paso 4.  Sistema de autenticación: login con bcrypt, sesiones por cookie,
           script CLI para añadir usuarios.
  Paso 5.  Cliente de Google Sheets: leer y escribir tickets.
  Paso 6.  Servicio de categorización con Anthropic API (Claude Haiku).
           Incluir tests unitarios sencillos.
  Paso 7.  Cliente Gmail API: leer mensajes nuevos, enviar emails.
  Paso 8.  Worker en background que junta todo: lee Gmail → categoriza →
           crea ticket → guarda en SQLite y Sheets → envía confirmación
           al cliente → notifica a soporte@telycoposio.com.
  Paso 9.  Web minimalista: login, listado de tickets con filtros, vista
           de detalle, formulario para responder al cliente.
  Paso 10. Despliegue: documentación paso a paso para instalarlo en un
           servidor Ubuntu. Backup automatizado del SQLite.

Cuando empezamos cada paso, hacemos esto:
  a) Me dices qué vas a crear/modificar y por qué.
  b) Me listas qué necesitas de mí ANTES de empezar (ej: "necesito que
     crees una API key en X y me la des", "necesito que decidas entre
     A y B").
  c) Programas.
  d) Me explicas cómo probarlo en mi máquina.
  e) Esperas mi visto bueno.

== Reglas de código ==

- Comentarios en español, código (variables/funciones) en inglés.
- Nada de código mágico. Si una línea no es obvia, lleva comentario.
- Nada de credenciales en el código. Todo va en .env (que NO se sube a git).
- Logs útiles: cada vez que se procesa un ticket, log con su ID y categoría.
  Nunca logear el cuerpo entero del email ni passwords.
- Manejo de errores explícito: si falla la API de Anthropic, el ticket se
  guarda con categoría "PENDIENTE_REVISION" y un humano lo revisa luego.
  Nunca perder un email entrante.
- Idempotencia: si por algún motivo procesamos dos veces el mismo email
  (mismo Message-ID), no duplicamos el ticket.

== Reglas de comunicación ==

- Si te pido algo y crees que es mala idea, dilo y propón alternativa.
- Si una librería que ibas a usar tiene una mejor alternativa, avísame.
- Si una API ha cambiado o no estás 100% seguro de su interfaz actual,
  dilo y busca documentación oficial antes de programar.
- Cuando termines algo, dame siempre un comando concreto para probarlo,
  no una descripción genérica.

== Para empezar ==

Primero, lee SPEC.md. Después, dime:

  1. Qué has entendido del proyecto (resumen breve).
  2. Qué necesitas que yo haga ANTES del Paso 1 (cuentas a crear, claves
     a generar, decisiones que faltan...).
  3. Si hay algo en SPEC.md que te parece mal o mejorable.

No empieces a programar nada hasta que yo te diga "vamos al Paso 1".
```

## 📋 (copiar hasta aquí ↑)

---

## Cómo usarlo

1. Crea una carpeta vacía donde quieras tener el proyecto, por ejemplo:
   ```
   C:\Users\TU_USUARIO\Proyectos\telycoposio-tickets
   ```

2. Copia dentro el archivo `SPEC.md` que te he generado.

3. Abre **VS Code** en esa carpeta.

4. Abre la terminal integrada de VS Code (`Ctrl+ñ` o `Ver → Terminal`).

5. Lanza Claude Code:
   ```
   claude
   ```

6. Pega el prompt de arriba (todo lo que está dentro del bloque de código) y pulsa Enter.

7. Claude leerá el SPEC, te resumirá lo que ha entendido y te dirá qué necesita de ti antes de empezar a programar.

8. A partir de ahí, **trabajáis paso a paso**. Tu rol es:
   - Validar lo que propone.
   - Crear las cuentas/credenciales que te pida.
   - Decir "vamos al Paso 2" cuando hayas verificado que el Paso 1 funciona.
