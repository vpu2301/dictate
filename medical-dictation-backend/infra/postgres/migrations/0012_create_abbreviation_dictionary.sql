-- Sprint 05 / Day 5 — per-tenant + global abbreviation dictionary.
--
-- ``tenant_id IS NULL`` → global rule (DBA / migration-managed).
-- ``tenant_id IS NOT NULL`` → tenant override; wins on collision.
--
-- The snapshot pattern (sprint-05) reads merged rules ONCE at NLP
-- request entry; admin edits don't affect in-flight processing.

CREATE TABLE abbreviation_dictionary (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID REFERENCES tenants(id),
    language        TEXT NOT NULL CHECK (language IN ('uk','en')),
    expanded        TEXT NOT NULL,
    abbreviated     TEXT NOT NULL,
    direction       TEXT NOT NULL CHECK (direction IN ('expand','compact','either')),
    domain          TEXT,                                  -- 'cardiology', 'all', NULL
    case_sensitive  BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, language, expanded, abbreviated)
);

CREATE INDEX abbrev_tenant_language_idx
    ON abbreviation_dictionary (tenant_id, language);
CREATE INDEX abbrev_global_language_idx
    ON abbreviation_dictionary (language)
    WHERE tenant_id IS NULL;

CREATE TRIGGER abbreviation_dictionary_set_updated_at
    BEFORE UPDATE ON abbreviation_dictionary
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

GRANT SELECT ON abbreviation_dictionary TO app_role;
GRANT INSERT, UPDATE, DELETE ON abbreviation_dictionary TO app_role;
-- tenant_writer also owns inserts of global rules via DBA migration.
GRANT INSERT, UPDATE, DELETE ON abbreviation_dictionary TO tenant_writer;

ALTER TABLE abbreviation_dictionary ENABLE ROW LEVEL SECURITY;
ALTER TABLE abbreviation_dictionary FORCE  ROW LEVEL SECURITY;

-- Read: own tenant rows OR global rows.
CREATE POLICY abbreviation_dictionary_read ON abbreviation_dictionary
    FOR SELECT TO app_role
    USING (
        tenant_id = current_setting('app.tenant_id', true)::uuid
        OR tenant_id IS NULL
    );

-- Write: only OWN tenant rows. Global rows are immutable through the
-- app role; DBAs use the tenant_writer role for migration seeds.
CREATE POLICY abbreviation_dictionary_write_own ON abbreviation_dictionary
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY abbreviation_dictionary_update_own ON abbreviation_dictionary
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY abbreviation_dictionary_delete_own ON abbreviation_dictionary
    FOR DELETE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- RESTRICTIVE defence in depth: a future PERMISSIVE policy can't accidentally
-- expose another tenant's rows.
CREATE POLICY abbreviation_dictionary_restrictive ON abbreviation_dictionary
    AS RESTRICTIVE FOR SELECT
    USING (
        tenant_id = current_setting('app.tenant_id', true)::uuid
        OR tenant_id IS NULL
    );
