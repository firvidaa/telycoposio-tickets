-- =====================================================================
-- Esquema SQLite — Telycoposio Tickets
-- =====================================================================
-- SQLite es la fuente de verdad operativa (ver SPEC.md §4 v1.2).
-- Google Sheets se sincroniza en background; no se referencia desde aqui.
--
-- Convenciones:
--   * BOOLEAN  -> INTEGER 0/1 con CHECK que lo asegura.
--   * DATETIME -> TEXT en ISO-8601 UTC ("2026-05-08T12:34:56+00:00").
--   * Las CHECK constraints duplican intencionadamente la validacion
--     que hace Pydantic en la capa de aplicacion: si algo se cuela por
--     SQL crudo (script, REPL, migracion manual), la BD aun protege los
--     invariantes.
-- =====================================================================

-- ---------------------------------------------------------------------
-- tickets
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tickets (
    id                          TEXT    PRIMARY KEY,
    created_at                  TEXT    NOT NULL,
    channel                     TEXT    NOT NULL
        CHECK (channel IN ('email', 'whatsapp', 'voicemail')),
    from_name                   TEXT,
    from_email                  TEXT,
    from_phone                  TEXT,
    subject                     TEXT    NOT NULL,
    body                        TEXT    NOT NULL,
    status                      TEXT    NOT NULL DEFAULT 'NEW'
        CHECK (status IN ('NEW', 'IN_PROGRESS', 'WAITING', 'CLOSED')),
    category                    TEXT
        CHECK (category IS NULL
               OR category IN ('ADMINISTRATIVO', 'COMERCIAL', 'SOPORTE')),
    category_confidence         REAL
        CHECK (category_confidence IS NULL
               OR (category_confidence >= 0.0 AND category_confidence <= 1.0)),
    category_reasoning          TEXT,
    category_manual_override    INTEGER NOT NULL DEFAULT 0
        CHECK (category_manual_override IN (0, 1)),
    needs_review                INTEGER NOT NULL DEFAULT 0
        CHECK (needs_review IN (0, 1)),
    raw_message_id              TEXT,
    attachments                 TEXT    NOT NULL DEFAULT '[]',
    client_notified_at          TEXT,
    last_updated_at             TEXT    NOT NULL,
    synced_to_sheets_at         TEXT
);

-- Indice unico parcial: dos tickets distintos no pueden compartir
-- raw_message_id, pero permitimos varios NULL (p.ej. canal voicemail).
CREATE UNIQUE INDEX IF NOT EXISTS idx_tickets_raw_message_id
    ON tickets(raw_message_id)
    WHERE raw_message_id IS NOT NULL;

-- Indices de filtros tipicos en la UI.
CREATE INDEX IF NOT EXISTS idx_tickets_status        ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_needs_review  ON tickets(needs_review);
CREATE INDEX IF NOT EXISTS idx_tickets_created_at    ON tickets(created_at);

-- Indice parcial para que el sincronizador encuentre rapido los pendientes.
CREATE INDEX IF NOT EXISTS idx_tickets_pending_sync
    ON tickets(synced_to_sheets_at)
    WHERE synced_to_sheets_at IS NULL;

-- ---------------------------------------------------------------------
-- ticket_replies (Paso 10)
-- ---------------------------------------------------------------------
-- Historial de respuestas enviadas al cliente desde la web. Inmutables:
-- una vez enviada (SMTP OK + INSERT), no se edita ni se borra.
--
-- FK a tickets.id con ON DELETE CASCADE: si en el futuro borraramos un
-- ticket (no es flujo MVP), las respuestas se van con el. user_id no se
-- cascadea para no perder el rastro si se borra un usuario.
CREATE TABLE IF NOT EXISTS ticket_replies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   TEXT    NOT NULL
        REFERENCES tickets(id) ON DELETE CASCADE,
    sent_at     TEXT    NOT NULL,
    user_id     INTEGER NOT NULL
        REFERENCES users(id),
    body        TEXT    NOT NULL,
    message_id  TEXT    NOT NULL
);

-- Indice para listar respuestas de un ticket en orden cronologico.
CREATE INDEX IF NOT EXISTS idx_ticket_replies_ticket_id
    ON ticket_replies(ticket_id, sent_at);

-- ---------------------------------------------------------------------
-- users
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT    NOT NULL UNIQUE,
    password_hash   TEXT    NOT NULL,
    display_name    TEXT    NOT NULL,
    email           TEXT,
    role            TEXT    NOT NULL DEFAULT 'user'
        CHECK (role IN ('user', 'admin')),
    created_at      TEXT    NOT NULL
);
