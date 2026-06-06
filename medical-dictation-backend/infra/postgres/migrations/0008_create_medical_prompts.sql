-- Sprint 03 / Day 3 — `medical_prompts`: clinician-authored seed prompts
-- per (language, specialty). Whisper accepts ≤ 224-token prompts to bias
-- towards in-domain vocabulary; each row holds one such prompt.
--
-- These prompts are GLOBAL, not tenant-scoped — every tenant draws from
-- the same catalogue in sprint 03. Sprint 17 adds tenant-specific
-- overrides via a nullable tenant_id column.

CREATE TABLE medical_prompts (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    language    TEXT NOT NULL CHECK (language IN ('uk','en')),
    specialty   TEXT NOT NULL,
    prompt_text TEXT NOT NULL,
    version     SMALLINT NOT NULL DEFAULT 1,
    is_default  BOOLEAN  NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Exactly one default per (language, specialty). Without the partial
-- unique index, two concurrent INSERTs marking is_default=true would
-- both succeed.
CREATE UNIQUE INDEX medical_prompts_lang_spec_active_idx
    ON medical_prompts (language, specialty) WHERE is_default;

CREATE INDEX medical_prompts_lang_idx ON medical_prompts (language);

-- Public catalogue: every authenticated role can SELECT.
GRANT SELECT ON medical_prompts TO app_role, audit_reader;
-- Writes go through tenant_writer for the sprint-03 pilot — there is no
-- per-tenant authorship yet. tenant_writer already exists from sprint 02.
GRANT INSERT, UPDATE, DELETE ON medical_prompts TO tenant_writer;

-- No RLS — prompts are not tenant data. Documented in ADR-0007 §
-- "exceptions to RLS-first": this is one of two (the other is `tenants`
-- itself, where tenant_writer manages cross-tenant rows).
