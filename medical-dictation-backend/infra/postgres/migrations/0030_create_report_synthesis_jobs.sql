-- Spec item 1 — report_synthesis_jobs.
--
-- A synthesis job is a *read-only* artefact: it records the per-section
-- synthesised text (raw dictation → clean prose) for one
-- (report, version, section set, language). It never mutates the report;
-- applying the result is done later via the draft PUT. Jobs are keyed by
-- ``request_hash`` for idempotent replay (same report version + section
-- set + language + body_hash → same job).

CREATE TABLE report_synthesis_jobs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    report_id           UUID NOT NULL REFERENCES reports(id) ON DELETE RESTRICT,

    -- The report version this synthesis ran against (part of the idem key).
    version_number      INTEGER NOT NULL,
    language            TEXT NOT NULL,

    -- [{section_key, original, text}] — both the original dictation and the
    -- synthesised text so the UI can diff/revert.
    sections_jsonb      JSONB NOT NULL DEFAULT '[]'::jsonb,

    -- sha256 over (report_id, version_number, sorted sections, language,
    -- body_hash of current content). Idempotency key.
    request_hash        TEXT NOT NULL,

    status              TEXT NOT NULL DEFAULT 'completed',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX report_synthesis_jobs_report_idx
    ON report_synthesis_jobs (tenant_id, report_id, created_at DESC);
-- Idempotency: one job per (tenant, report, request_hash).
CREATE UNIQUE INDEX report_synthesis_jobs_idem_idx
    ON report_synthesis_jobs (tenant_id, report_id, request_hash);

-- ── RLS ─────────────────────────────────────────────────────────────

ALTER TABLE report_synthesis_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE report_synthesis_jobs FORCE  ROW LEVEL SECURITY;

CREATE POLICY report_synthesis_jobs_tenant_select ON report_synthesis_jobs
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY report_synthesis_jobs_tenant_insert ON report_synthesis_jobs
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY report_synthesis_jobs_tenant_update ON report_synthesis_jobs
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY report_synthesis_jobs_tenant_delete ON report_synthesis_jobs
    FOR DELETE TO app_role
    USING (false);  -- read-only artefact; never hard-deleted by the app

-- Defence-in-depth RESTRICTIVE: tenant scope enforced even if a future
-- PERMISSIVE policy is mis-written.
CREATE POLICY report_synthesis_jobs_tenant_restrictive ON report_synthesis_jobs
    AS RESTRICTIVE
    FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT ON report_synthesis_jobs TO app_role;
