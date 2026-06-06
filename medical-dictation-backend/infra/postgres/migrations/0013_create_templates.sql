-- Sprint 06 / Day 1 — `templates`: clinical content surface for sprint 8 reports.
--
-- One row per (tenant, code, schema_version) pair. tenant_id IS NULL
-- denotes a SYSTEM template (visible to all tenants; managed via DBA
-- migration only). A tenant clones a system template by INSERTing a
-- row with `parent_template_id` set and `tenant_id` = its own.
--
-- Versioning:
--   - Cosmetic edit (name / aliases / asr_prompt / order / default_content /
--     metadata): UPDATE in place; schema_version bumps.
--   - Structural edit (section added/removed/field_type-changed/
--     required-flipped/min_chars-tightened): INSERT new row with
--     parent_template_id set + schema_version reset to 1.
--
-- Lifecycle:
--   draft → active → deprecated. No hard delete (sprint 8 reports
--   reference templates by id; references must outlive deprecation).

CREATE TABLE templates (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID REFERENCES tenants(id),         -- NULL = system
    parent_template_id  UUID REFERENCES templates(id),       -- structural-edit lineage
    code                TEXT NOT NULL,                       -- "cardiology_outpatient"
    name                TEXT NOT NULL,
    language            TEXT NOT NULL CHECK (language IN ('uk','en')),
    specialty           TEXT NOT NULL,
    schema_version      SMALLINT NOT NULL DEFAULT 1 CHECK (schema_version >= 1),
    is_system           BOOLEAN NOT NULL DEFAULT FALSE,
    status              TEXT NOT NULL CHECK (status IN ('draft','active','deprecated'))
                            DEFAULT 'draft',
    schema_jsonb        JSONB NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, code, schema_version)
);

CREATE INDEX idx_templates_tenant_specialty
    ON templates (tenant_id, specialty, language)
    WHERE status <> 'deprecated';
CREATE INDEX idx_templates_system_specialty
    ON templates (specialty, language)
    WHERE tenant_id IS NULL AND status <> 'deprecated';
CREATE INDEX idx_templates_parent
    ON templates (parent_template_id)
    WHERE parent_template_id IS NOT NULL;

CREATE TRIGGER templates_set_updated_at
    BEFORE UPDATE ON templates
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ── Grants ───────────────────────────────────────────────────────────
GRANT SELECT, INSERT, UPDATE ON templates TO app_role;
-- System rows (tenant_id IS NULL) are inserted only by tenant_writer
-- via DBA migration. app_role's RESTRICTIVE policy below enforces this.
GRANT SELECT, INSERT, UPDATE ON templates TO tenant_writer;

-- ── Row-level security ──────────────────────────────────────────────
ALTER TABLE templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE templates FORCE  ROW LEVEL SECURITY;

-- Visibility: own-tenant rows OR system rows (tenant_id IS NULL).
CREATE POLICY templates_visibility ON templates
    FOR SELECT TO app_role
    USING (
        tenant_id = current_setting('app.tenant_id', true)::uuid
        OR tenant_id IS NULL
    );

-- Writes (INSERT/UPDATE) — restrict to own-tenant rows only. Defense
-- in depth: a future PERMISSIVE policy can't accidentally allow a
-- tenant to insert a row with tenant_id = NULL (system) or another
-- tenant's UUID.
CREATE POLICY templates_write ON templates
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY templates_update ON templates
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY templates_restrictive ON templates
    AS RESTRICTIVE FOR SELECT
    USING (
        tenant_id = current_setting('app.tenant_id', true)::uuid
        OR tenant_id IS NULL
    );

-- tenant_writer (DBA-role for migrations) has unrestricted access to
-- write system rows.
CREATE POLICY templates_tenant_writer_all ON templates
    FOR ALL TO tenant_writer
    USING (true)
    WITH CHECK (true);

-- ── Sprint 8 / 11 readiness — templates are referenced by sprint-8 reports.
-- The hard-delete prevention is enforced by application code
-- (DELETE not granted to app_role for this table; only soft-delete via
-- UPDATE status='deprecated'). The FK with ON DELETE RESTRICT will be
-- added by sprint 8 migration on `reports.template_id`.
REVOKE DELETE ON templates FROM app_role, tenant_writer;
