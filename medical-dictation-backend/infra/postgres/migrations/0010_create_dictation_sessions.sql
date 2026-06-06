-- Sprint 04 / Day 1 — `dictation_sessions`: the row that records every
-- streaming dictation lifecycle.
--
-- Wire protocol notes (see docs/api/dictation-ws-v1.md):
--   - status transitions: creating → active → (paused ⇄ active) →
--     (reconnecting → active) → finalized | abandoned | failed
--   - transcript_jsonb holds the committed (final) tokens; partials
--     are NOT persisted. Sprint 05 NLP annotations land here too.
--   - audio_file_id is NULL until finalize; on abandon it stays NULL.
--   - worker_id is the dictation/asr-worker that owns the live inference
--     state; on worker death the session moves to failed (or abandoned
--     if the user never reconnects).

CREATE TABLE dictation_sessions (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            UUID NOT NULL REFERENCES tenants(id),
    user_id              UUID NOT NULL,                    -- Keycloak sub
    encounter_id         UUID,                             -- sprint 11 FK; nullable
    target_kind          TEXT NOT NULL CHECK (target_kind IN
                              ('clinical_note','anamnesis','referral','generic'))
                              DEFAULT 'generic',
    template_id          UUID,                             -- sprint 06 FK
    language             TEXT NOT NULL CHECK (language IN ('uk','en')),
    prompt_id            UUID NOT NULL REFERENCES medical_prompts(id),
    model                TEXT NOT NULL DEFAULT 'large-v3',
    worker_id            TEXT,                             -- set on accept

    status               TEXT NOT NULL CHECK (status IN
                              ('creating','active','paused','reconnecting',
                               'finalized','abandoned','failed'))
                              DEFAULT 'creating',

    transcript_jsonb     JSONB NOT NULL DEFAULT '[]'::jsonb,
    audio_file_id        UUID REFERENCES audio_files(id),

    -- Timing / quality metrics (populated incrementally)
    total_audio_ms       INTEGER NOT NULL DEFAULT 0,
    total_speech_ms      INTEGER NOT NULL DEFAULT 0,
    avg_partial_latency_ms INTEGER,
    avg_final_latency_ms INTEGER,
    rtf                  NUMERIC(6,3),

    -- Resilience counters
    network_drop_count   INTEGER NOT NULL DEFAULT 0,
    truncated            BOOLEAN NOT NULL DEFAULT false,

    -- Failure detail (for status=failed)
    error_kind           TEXT,
    error_detail         TEXT,

    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at           TIMESTAMPTZ,
    last_active_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finalized_at         TIMESTAMPTZ,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX dictation_sessions_tenant_user_idx
    ON dictation_sessions (tenant_id, user_id, created_at DESC);
CREATE INDEX dictation_sessions_status_idx
    ON dictation_sessions (status)
    WHERE status IN ('active','paused','reconnecting','creating');
CREATE INDEX dictation_sessions_worker_idx
    ON dictation_sessions (worker_id)
    WHERE worker_id IS NOT NULL;
CREATE INDEX dictation_sessions_resume_idx
    ON dictation_sessions (tenant_id, user_id, last_active_at DESC)
    WHERE status IN ('active','paused','reconnecting');

CREATE TRIGGER dictation_sessions_set_updated_at
    BEFORE UPDATE ON dictation_sessions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

GRANT SELECT, INSERT, UPDATE ON dictation_sessions TO app_role;

ALTER TABLE dictation_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE dictation_sessions FORCE  ROW LEVEL SECURITY;

CREATE POLICY dictation_sessions_tenant_select ON dictation_sessions
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY dictation_sessions_tenant_insert ON dictation_sessions
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY dictation_sessions_tenant_update ON dictation_sessions
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY dictation_sessions_tenant_restrictive ON dictation_sessions
    AS RESTRICTIVE FOR ALL
    USING      (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
