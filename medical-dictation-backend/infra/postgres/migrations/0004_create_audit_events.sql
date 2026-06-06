-- Sprint 02 / Day 4 — `audit.events`: tamper-evident, hash-chained event log.
--
-- Append-only by construction:
--   - Triggers raise on UPDATE and DELETE.
--   - app_role and tenant_writer have NO insert privilege; only audit_writer.
--   - The (tenant_id, seq) PK + `prev_hash` field form a per-tenant Merkle
--     chain. The verifier (Day 5) walks the chain and detects tampering by
--     comparing the recomputed hash to the stored payload_hash.
--
-- Note: the `audit` schema and writer/reader roles are provisioned in
-- infra/postgres/init.sql; this migration adds the table and policies.

CREATE TABLE audit.events (
    tenant_id    UUID            NOT NULL,
    seq          BIGINT          NOT NULL,
    created_at   TIMESTAMPTZ     NOT NULL DEFAULT clock_timestamp(),
    actor_sub    UUID,                   -- nullable for system / unauthenticated events
    actor_role   TEXT,                   -- denormalized at write
    kind         TEXT            NOT NULL,   -- e.g., 'auth.login', 'rls.bypass'
    target_kind  TEXT,                   -- e.g., 'user', 'report'
    target_id    TEXT,                   -- nullable
    payload_jcs  JSONB           NOT NULL,   -- canonical event record
    prev_hash    BYTEA,                  -- NULL only for seq=1 (genesis = 32 zero bytes)
    payload_hash BYTEA           NOT NULL,   -- sha256(prev_hash || jcs_bytes(payload_jcs))
    severity     TEXT            NOT NULL CHECK (severity IN ('info','warn','sec','error'))
                 DEFAULT 'info',
    PRIMARY KEY (tenant_id, seq)
);

CREATE INDEX events_tenant_created_idx ON audit.events (tenant_id, created_at DESC);
CREATE INDEX events_tenant_kind_idx    ON audit.events (tenant_id, kind);
CREATE INDEX events_tenant_actor_idx   ON audit.events (tenant_id, actor_sub) WHERE actor_sub IS NOT NULL;

-- ── Grants ───────────────────────────────────────────────────────────
-- Belt-and-braces: revoke from roles that have NO business touching this.
REVOKE ALL ON audit.events FROM PUBLIC, app_role, tenant_writer;
-- audit_writer needs SELECT, INSERT, and UPDATE. The UPDATE privilege is
-- required *only* so the writer can acquire row locks via
-- `SELECT seq, payload_hash ... FOR UPDATE` when computing prev_hash.
-- Actual UPDATE statements are blocked by the `events_no_update` trigger
-- (defence in depth: the trigger is the real immutability enforcer).
GRANT  INSERT, SELECT, UPDATE ON audit.events TO audit_writer;
GRANT  SELECT                 ON audit.events TO audit_reader;

-- ── Row-level security ───────────────────────────────────────────────
ALTER TABLE audit.events ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit.events FORCE  ROW LEVEL SECURITY;

-- Reads: tenant-scoped (both audit_writer and audit_reader honour app.tenant_id).
CREATE POLICY audit_events_read ON audit.events
    FOR SELECT TO audit_writer, audit_reader
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- Writes: only audit_writer, and only for the tenant set on the connection.
CREATE POLICY audit_events_writer_insert ON audit.events
    FOR INSERT TO audit_writer
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- SELECT FOR UPDATE under RLS requires a matching UPDATE policy *in addition*
-- to the SELECT one. Without this, the writer's `... ORDER BY seq DESC LIMIT 1
-- FOR UPDATE` returns no rows even when rows exist, and the next write attempts
-- seq=1 → primary-key violation. Actual UPDATE attempts are still blocked by the
-- `events_no_update` trigger (defence in depth).
CREATE POLICY audit_events_writer_lock ON audit.events
    FOR UPDATE TO audit_writer
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- RESTRICTIVE defence in depth — applies regardless of any future PERMISSIVE
-- policy a maintainer might add.
CREATE POLICY audit_events_tenant_restrictive ON audit.events
    AS RESTRICTIVE FOR ALL
    USING      (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- ── Immutability trigger ─────────────────────────────────────────────
-- Even the audit_writer role cannot UPDATE or DELETE a row once committed.
-- Only the DBA superuser can — and that action is itself audited at the
-- Postgres-log level.
CREATE OR REPLACE FUNCTION audit.events_immutable() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit.events is immutable (operation=%, row_seq=%, tenant=%)',
        TG_OP, COALESCE(OLD.seq::text, NEW.seq::text), COALESCE(OLD.tenant_id::text, NEW.tenant_id::text)
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER events_no_update
    BEFORE UPDATE ON audit.events
    FOR EACH ROW EXECUTE FUNCTION audit.events_immutable();

CREATE TRIGGER events_no_delete
    BEFORE DELETE ON audit.events
    FOR EACH ROW EXECUTE FUNCTION audit.events_immutable();
