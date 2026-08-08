-- 0068_evidence_ingest.sql
-- EVA-S02: ingestion job tables (resumable pipeline, dead-letter, quarantine)
-- and the GLOBAL corpus read amendment.
--
-- GLOBAL_TENANT: the reserved nil uuid 00000000-0000-0000-0000-000000000000
-- owns the shared global corpus (WHO/НІЦЕ-open/PubMed/drug_reference rows).
-- Corpus tables become readable by every tenant for global rows; writes stay
-- strictly tenant-scoped (ingesting global content requires the operator to
-- scope the connection to the nil uuid explicitly). Q&A tables are untouched.

-- The reserved global-corpus owner needs a tenants row: tenant_keks (crypto)
-- and future FKs reference tenants. Inactive + 'reserved' status keeps it out
-- of every operator surface (memberships are what make tenants visible).
INSERT INTO tenants (id, name, display_name, locale, status, is_active)
VALUES ('00000000-0000-0000-0000-000000000000', 'evidence-global-corpus',
        'Evidence Global Corpus (reserved)', 'uk', 'suspended', false)
ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------- ingest tables

CREATE TABLE ingest_jobs (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid NOT NULL,
    source_uri       text NOT NULL,
    kind             text NOT NULL CHECK (kind IN
                       ('pdf', 'pmc_xml', 'html_guideline', 'markdown', 'docx')),
    state            text NOT NULL DEFAULT 'queued' CHECK (state IN
                       ('queued', 'parsing', 'chunking', 'enriching', 'embedding',
                        'indexing', 'done', 'dead', 'quarantined')),
    attempts         integer NOT NULL DEFAULT 0,
    last_error       text,
    idempotence_key  text NOT NULL,
    metadata_overrides jsonb NOT NULL DEFAULT '{}',
    timings          jsonb NOT NULL DEFAULT '{}',
    document_id      uuid,
    document_version_id uuid,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, idempotence_key)
);

CREATE INDEX ingest_jobs_state_idx ON ingest_jobs (tenant_id, state, created_at);

CREATE TABLE ingest_errors (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid NOT NULL,
    job_id       uuid NOT NULL REFERENCES ingest_jobs (id) ON DELETE CASCADE,
    stage        text NOT NULL,
    error        text NOT NULL,
    payload_ref  text,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ingest_errors_job_idx ON ingest_errors (tenant_id, job_id);

CREATE TABLE quarantine (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL,
    job_id        uuid NOT NULL REFERENCES ingest_jobs (id),
    document_ref  text NOT NULL,
    reason        text NOT NULL,
    patterns      jsonb NOT NULL DEFAULT '[]',
    reviewed_by   uuid,
    decision      text CHECK (decision IN ('approved', 'rejected')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    reviewed_at   timestamptz
);

CREATE INDEX quarantine_open_idx ON quarantine (tenant_id, created_at)
    WHERE decision IS NULL;

-- RLS (platform idiom: permissive per-command + restrictive catch-all)

ALTER TABLE ingest_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE ingest_jobs FORCE  ROW LEVEL SECURITY;
CREATE POLICY ingest_jobs_tenant_select ON ingest_jobs
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY ingest_jobs_tenant_insert ON ingest_jobs
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY ingest_jobs_tenant_update ON ingest_jobs
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY ingest_jobs_tenant_delete ON ingest_jobs
    FOR DELETE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY ingest_jobs_tenant_restrictive ON ingest_jobs
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON ingest_jobs TO app_role;

ALTER TABLE ingest_errors ENABLE ROW LEVEL SECURITY;
ALTER TABLE ingest_errors FORCE  ROW LEVEL SECURITY;
CREATE POLICY ingest_errors_tenant_select ON ingest_errors
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY ingest_errors_tenant_insert ON ingest_errors
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY ingest_errors_tenant_delete ON ingest_errors
    FOR DELETE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY ingest_errors_tenant_restrictive ON ingest_errors
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT, INSERT, DELETE ON ingest_errors TO app_role;

ALTER TABLE quarantine ENABLE ROW LEVEL SECURITY;
ALTER TABLE quarantine FORCE  ROW LEVEL SECURITY;
CREATE POLICY quarantine_tenant_select ON quarantine
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY quarantine_tenant_insert ON quarantine
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY quarantine_tenant_update ON quarantine
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY quarantine_tenant_restrictive ON quarantine
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT, INSERT, UPDATE ON quarantine TO app_role;

-- ------------------------------------------------- global corpus readability

-- Corpus tables (0067): global rows (nil-uuid tenant) become readable by every
-- tenant. SELECT policies and the RESTRICTIVE catch-alls are recreated with
-- the global disjunct; INSERT/UPDATE/DELETE policies keep strict tenant match,
-- so no tenant can write or delete global rows without explicitly scoping to
-- the nil uuid (operator-only path).

DROP POLICY documents_tenant_select ON documents;
CREATE POLICY documents_tenant_select ON documents
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid
           OR tenant_id = '00000000-0000-0000-0000-000000000000'::uuid);
DROP POLICY documents_tenant_restrictive ON documents;
CREATE POLICY documents_tenant_restrictive ON documents
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid
           OR tenant_id = '00000000-0000-0000-0000-000000000000'::uuid);

DROP POLICY document_versions_tenant_select ON document_versions;
CREATE POLICY document_versions_tenant_select ON document_versions
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid
           OR tenant_id = '00000000-0000-0000-0000-000000000000'::uuid);
DROP POLICY document_versions_tenant_restrictive ON document_versions;
CREATE POLICY document_versions_tenant_restrictive ON document_versions
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid
           OR tenant_id = '00000000-0000-0000-0000-000000000000'::uuid);

-- The embedding stage backfills chunks.embedding after insert (resumable at
-- chunk granularity) — 0067 shipped chunks without UPDATE. Strict tenant
-- match: the operator scopes to the nil uuid when embedding global corpus.
CREATE POLICY chunks_tenant_update ON chunks
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT UPDATE ON chunks TO app_role;

DROP POLICY chunks_tenant_select ON chunks;
CREATE POLICY chunks_tenant_select ON chunks
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid
           OR tenant_id = '00000000-0000-0000-0000-000000000000'::uuid);
DROP POLICY chunks_tenant_restrictive ON chunks;
CREATE POLICY chunks_tenant_restrictive ON chunks
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid
           OR tenant_id = '00000000-0000-0000-0000-000000000000'::uuid);

DROP POLICY corpus_snapshots_tenant_select ON corpus_snapshots;
CREATE POLICY corpus_snapshots_tenant_select ON corpus_snapshots
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid
           OR tenant_id = '00000000-0000-0000-0000-000000000000'::uuid);
DROP POLICY corpus_snapshots_tenant_restrictive ON corpus_snapshots;
CREATE POLICY corpus_snapshots_tenant_restrictive ON corpus_snapshots
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid
           OR tenant_id = '00000000-0000-0000-0000-000000000000'::uuid);
