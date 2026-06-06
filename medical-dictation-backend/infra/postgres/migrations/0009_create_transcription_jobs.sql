-- Sprint 03 / Day 3 — `transcription_jobs`: durable record of every ASR
-- job submitted. The Redis Streams queue is the *transport*; this table
-- is the system of record (idempotency, audit, status, retries).
--
-- Lifecycle:
--   queued → running → complete | failed | cancelled
--
-- The worker uses (tenant_id, id) as the dedupe key against duplicate
-- delivery from Redis Streams (XAUTOCLAIM re-delivers on stuck consumers).
-- A row marked `complete` or `failed` short-circuits the worker.

CREATE TABLE transcription_jobs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id),
    audio_id            UUID NOT NULL REFERENCES audio_files(id),
    requester_sub       UUID NOT NULL,
    prompt_id           UUID NOT NULL REFERENCES medical_prompts(id),
    language            TEXT NOT NULL CHECK (language IN ('uk','en')),
    model               TEXT NOT NULL DEFAULT 'large-v3',
    status              TEXT NOT NULL CHECK (status IN
                            ('queued','running','complete','failed','cancelled'))
                            DEFAULT 'queued',
    result_storage_uri  TEXT,
    error_kind          TEXT,
    error_detail        TEXT,
    cancel_requested    BOOLEAN NOT NULL DEFAULT false,
    queued_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ,
    attempts            SMALLINT NOT NULL DEFAULT 0,
    metadata            JSONB
);

CREATE INDEX transcription_jobs_tenant_status_idx ON transcription_jobs (tenant_id, status);
CREATE INDEX transcription_jobs_audio_idx        ON transcription_jobs (audio_id);
CREATE INDEX transcription_jobs_tenant_queued_idx
    ON transcription_jobs (tenant_id, queued_at DESC);

-- ── Grants ───────────────────────────────────────────────────────────
GRANT SELECT, INSERT, UPDATE ON transcription_jobs TO app_role;

-- ── Row-level security ───────────────────────────────────────────────
ALTER TABLE transcription_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE transcription_jobs FORCE  ROW LEVEL SECURITY;

CREATE POLICY transcription_jobs_tenant_select ON transcription_jobs
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY transcription_jobs_tenant_insert ON transcription_jobs
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY transcription_jobs_tenant_update ON transcription_jobs
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY transcription_jobs_tenant_restrictive ON transcription_jobs
    AS RESTRICTIVE FOR ALL
    USING      (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
