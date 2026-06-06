-- Sprint 05 / Day 2 — voice command catalogue.
--
-- Global (not tenant-scoped). Sprint 17 may add per-tenant overrides.
-- The matcher's vocabulary is loaded from this table on nlp-service
-- startup; updates land via DB seed (data, not code).

CREATE TABLE voice_commands (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    intent                   TEXT NOT NULL,
    language                 TEXT NOT NULL CHECK (language IN ('uk','en')),
    phrases                  JSONB NOT NULL,                    -- list of word-lists
    requires_pause_before_ms INTEGER NOT NULL DEFAULT 200,
    min_avg_probability      REAL NOT NULL DEFAULT 0.85,
    is_section_command       BOOLEAN NOT NULL DEFAULT FALSE,
    is_active                BOOLEAN NOT NULL DEFAULT TRUE,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX voice_commands_language_active_idx
    ON voice_commands (language, is_active);

-- Public catalogue; both app_role and audit_reader read it.
GRANT SELECT ON voice_commands TO app_role, audit_reader;
GRANT INSERT, UPDATE, DELETE ON voice_commands TO tenant_writer;
-- No RLS: the catalogue is global. Per-tenant overrides come in sprint 17.
