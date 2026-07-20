-- Sprint 12 / Day 8 — daily-digest idempotency marker.
--
-- Same role as `autocomplete_rollup_progress` (sprint 10): a claim row
-- that makes the digest job safe to re-run. Two differences, both
-- deliberate:
--
--   1. It carries `tenant_id` and is FULLY RLS-guarded. The sprint-10
--      table took an RLS exemption that is still flagged for security /
--      DPO sign-off (Sprint A1 report); there is no reason to add a
--      second one. The digest job already runs per-tenant under
--      `tenant_connection`, so the predicate costs nothing.
--
--   2. The marker is CLAIMED BEFORE the work, not written after it.
--      Sprint 10's rollup does check-then-act — SELECT the marker, do
--      the work, INSERT the marker — which lets two concurrent runners
--      both pass the check and double-count before either inserts. For
--      a digest the equivalent race sends the same user two emails
--      (E6). Here the job INSERTs the marker first and treats a
--      unique-violation as "another worker owns this user-day", so the
--      claim is atomic.

CREATE TABLE notification_digest_progress (
    digest_date             DATE NOT NULL,
    tenant_id               UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    user_id                 UUID NOT NULL REFERENCES users(sub) ON DELETE CASCADE,

    -- Set when the claim is taken; the row exists before the send.
    claimed_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Set once the digest is actually sent. A row with claimed_at set but
    -- finished_at NULL and an old claim is a crashed run — the runbook's
    -- signal to investigate rather than blindly re-send.
    finished_at             TIMESTAMPTZ,

    notifications_included  INT NOT NULL DEFAULT 0,

    PRIMARY KEY (digest_date, tenant_id, user_id)
);

CREATE INDEX notification_digest_progress_unfinished_idx
    ON notification_digest_progress (digest_date, tenant_id)
    WHERE finished_at IS NULL;

ALTER TABLE notification_digest_progress ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_digest_progress FORCE  ROW LEVEL SECURITY;

-- app_role gets INSERT/UPDATE as well as SELECT because the digest job
-- currently runs INSIDE notification-service, on the app_role pool.
-- `app_notification_worker` below is the role that job will use once the
-- workers are split into their own deploy unit; until a DSN exists for
-- it (it is NOLOGIN by design) app_role is the actual writer, and
-- granting only the worker would make the job fail at runtime while
-- looking correct in review.
--
-- No DELETE for either role: re-running a day must be a deliberate
-- operator action (see docs/runbooks/notifications.md), not something
-- the job can do to itself.
CREATE POLICY notification_digest_progress_tenant_select ON notification_digest_progress
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY notification_digest_progress_tenant_insert ON notification_digest_progress
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY notification_digest_progress_tenant_update ON notification_digest_progress
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY notification_digest_progress_tenant_restrictive ON notification_digest_progress
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE ON notification_digest_progress TO app_role;

-- The digest job owns this table.
GRANT SELECT, INSERT, UPDATE ON notification_digest_progress TO app_notification_worker;
CREATE POLICY notification_digest_progress_worker_select ON notification_digest_progress
    FOR SELECT TO app_notification_worker
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY notification_digest_progress_worker_insert ON notification_digest_progress
    FOR INSERT TO app_notification_worker
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY notification_digest_progress_worker_update ON notification_digest_progress
    FOR UPDATE TO app_notification_worker
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY notification_digest_progress_worker_restrictive ON notification_digest_progress
    AS RESTRICTIVE FOR ALL TO app_notification_worker
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
