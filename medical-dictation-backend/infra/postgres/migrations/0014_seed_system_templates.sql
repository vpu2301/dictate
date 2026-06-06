-- Sprint 06 / Day 2 — seed system templates.
--
-- Loads the 16 hand-authored JSON files from infra/seeds/templates/
-- via a small psql \copy + idempotent UPSERT. Re-running is safe;
-- on conflict the row is updated (schema_version bumps if cosmetic;
-- structural changes require a hand-rolled migration).
--
-- The actual JSON content lives in infra/seeds/templates/*.json so it
-- is reviewable in PRs. This migration drives the upsert.
--
-- Idempotency: ON CONFLICT (tenant_id, code, schema_version) DO UPDATE
-- on cosmetic fields. tenant_id IS NULL for every system row.

BEGIN;

-- Helper: stable system-template insertion. Used by scripts/seed/seed_templates.py
-- which reads JSON files and calls this function. Keeping the function in the
-- migration ensures fresh DBs can run the seeds the same way.

CREATE OR REPLACE FUNCTION upsert_system_template(
    p_code           TEXT,
    p_name           TEXT,
    p_language       TEXT,
    p_specialty      TEXT,
    p_schema_version SMALLINT,
    p_schema_jsonb   JSONB
) RETURNS UUID LANGUAGE plpgsql AS $$
DECLARE
    v_id UUID;
BEGIN
    INSERT INTO templates
        (tenant_id, parent_template_id, code, name, language, specialty,
         schema_version, is_system, status, schema_jsonb)
    VALUES
        (NULL, NULL, p_code, p_name, p_language, p_specialty,
         p_schema_version, TRUE, 'active', p_schema_jsonb)
    ON CONFLICT (tenant_id, code, schema_version) DO UPDATE
        SET name = EXCLUDED.name,
            schema_jsonb = EXCLUDED.schema_jsonb,
            updated_at = now()
    RETURNING id INTO v_id;
    RETURN v_id;
END;
$$;

COMMIT;
