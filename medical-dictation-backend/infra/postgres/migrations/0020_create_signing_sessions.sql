-- Sprint 09 / Day 2 — signing_sessions.
--
-- Lifecycle row for an in-flight signing attempt. Each row terminates
-- in one of: signed (envelope created, signed_envelope_id set),
-- rejected, expired, failed.

CREATE TYPE signing_session_status AS ENUM (
    'initiating', 'awaiting_user', 'verifying',
    'signed', 'rejected', 'expired', 'failed'
);

CREATE TABLE signing_sessions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    initiated_by            UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,

    resource_type           TEXT NOT NULL,
    resource_id             UUID NOT NULL,
    resource_version_id     UUID NOT NULL,

    provider                signing_provider NOT NULL,
    provider_session_id     TEXT NOT NULL,

    status                  signing_session_status NOT NULL DEFAULT 'initiating',
    failure_reason          TEXT,
    expires_at              TIMESTAMPTZ NOT NULL,
    last_state_change       TIMESTAMPTZ NOT NULL DEFAULT now(),

    signed_envelope_id      UUID REFERENCES signed_envelopes(id) ON DELETE RESTRICT,

    callback_completion_url TEXT,
    redirect_url            TEXT,
    qr_payload              TEXT,

    purpose_code            TEXT,

    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT signing_sessions_provider_session_unique
        UNIQUE (provider, provider_session_id)
);

CREATE INDEX signing_sessions_tenant_status_idx
    ON signing_sessions (tenant_id, status, created_at DESC);
CREATE INDEX signing_sessions_expires_at_idx
    ON signing_sessions (expires_at)
    WHERE status IN ('initiating', 'awaiting_user', 'verifying');

ALTER TABLE signing_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE signing_sessions FORCE  ROW LEVEL SECURITY;

CREATE POLICY signing_sessions_tenant_select ON signing_sessions
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY signing_sessions_tenant_insert ON signing_sessions
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY signing_sessions_tenant_update ON signing_sessions
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY signing_sessions_tenant_delete ON signing_sessions
    FOR DELETE TO app_role
    USING (false);
CREATE POLICY signing_sessions_tenant_restrictive ON signing_sessions
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE ON signing_sessions TO app_role;

-- Callback writer role: the public callback endpoint runs the lookup
-- by ``provider_session_id`` first (no tenant context yet), then
-- transitions the row. Granted via SECURITY DEFINER function in
-- the service layer.
CREATE ROLE app_callback_writer;
GRANT SELECT, UPDATE ON signing_sessions TO app_callback_writer;
