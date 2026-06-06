-- Sprint 03 / Day 3 — `audio_files`: the row that catalogues every PHI
-- audio object stored in MinIO.
--
-- The actual bytes live in MinIO (encrypted via the envelope, no plaintext
-- ever at rest). This row stores the metadata required for downstream
-- jobs, retention, audit, and DSAR / right-to-erasure flows.
--
-- ``envelope_metadata`` is the JSON header from libs/crypto's EnvelopeBlob
-- — every field EXCEPT the actual ciphertext. We persist it on the row
-- so the worker doesn't have to parse the object header before deciding
-- whether to fetch the body.

CREATE TABLE audio_files (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES tenants(id),
    uploader_sub      UUID NOT NULL,                       -- Keycloak sub of submitter
    encounter_id      UUID,                                -- nullable until sprint 11
    mime_type         TEXT NOT NULL,
    size_bytes        BIGINT NOT NULL CHECK (size_bytes > 0),
    duration_ms       INTEGER,                             -- nullable until probed
    sha256            BYTEA NOT NULL,
    envelope_metadata JSONB NOT NULL,
    storage_uri       TEXT  NOT NULL,
    status            TEXT  NOT NULL CHECK (status IN
                          ('uploading','stored','transcribing','transcribed','deleted','failed'))
                          DEFAULT 'stored',
    retention_until   TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX audio_files_tenant_created_idx ON audio_files (tenant_id, created_at DESC);
CREATE INDEX audio_files_status_idx
    ON audio_files (status) WHERE status IN ('uploading','transcribing');
CREATE INDEX audio_files_tenant_uploader_idx ON audio_files (tenant_id, uploader_sub);
CREATE INDEX audio_files_retention_idx
    ON audio_files (retention_until) WHERE retention_until IS NOT NULL;

-- Updated-at trigger reuses the function created in 0001_create_tenants.
CREATE TRIGGER audio_files_set_updated_at
    BEFORE UPDATE ON audio_files
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ── Grants ───────────────────────────────────────────────────────────
GRANT SELECT, INSERT, UPDATE         ON audio_files TO app_role;
-- DELETE is reserved for the right-to-erasure flow (sprint 11) and runs
-- under a dedicated role. Not granted here.

-- ── Row-level security ───────────────────────────────────────────────
ALTER TABLE audio_files ENABLE ROW LEVEL SECURITY;
ALTER TABLE audio_files FORCE  ROW LEVEL SECURITY;

CREATE POLICY audio_files_tenant_select ON audio_files
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY audio_files_tenant_insert ON audio_files
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY audio_files_tenant_update ON audio_files
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY audio_files_tenant_restrictive ON audio_files
    AS RESTRICTIVE FOR ALL
    USING      (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
