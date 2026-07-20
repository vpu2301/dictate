-- Sprint 12 / Day 3 — `notification_outbox`: one row per (notification,
-- channel) delivery attempt.
--
-- The outbox is what makes delivery auditable rather than merely
-- attempted. Every channel a notification COULD have gone out on gets a
-- row, including the ones preference resolution decided to suppress —
-- those are written `suppressed` and never dispatched. That turns "the
-- user says they got an email after switching email off" from an
-- unfalsifiable claim into a lookup (E8).
--
-- UNIQUE (notification_id, channel) is the delivery-idempotency anchor:
-- re-driving the worker over the same row cannot produce a second send
-- (E3). Status transitions are taken under a row lock (SELECT ... FOR
-- UPDATE SKIP LOCKED) so two workers cannot both claim one row.

CREATE TABLE notification_outbox (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,

    notification_id     UUID NOT NULL
                            REFERENCES notifications(id) ON DELETE CASCADE,

    channel             TEXT NOT NULL
                            CHECK (channel IN ('in_app', 'email')),

    status              TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'sent', 'failed',
                                              'suppressed', 'dead')),

    attempt_count       INT NOT NULL DEFAULT 0,

    -- The retry worker's claim predicate. Always set for `pending`;
    -- NULL once the row reaches a terminal state.
    next_attempt_at     TIMESTAMPTZ DEFAULT now(),

    last_error          TEXT NOT NULL DEFAULT '',

    -- Provider-side id (SMTP Message-ID). Lets an operator correlate a
    -- complaint with a specific send without reading the body.
    provider_message_id TEXT NOT NULL DEFAULT '',

    -- Why a `suppressed` row was suppressed: 'preference', 'quiet_hours',
    -- 'no_email_address', 'digest_deferred'. Empty for other statuses.
    suppressed_reason   TEXT NOT NULL DEFAULT '',

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The delivery-idempotency anchor.
CREATE UNIQUE INDEX notification_outbox_channel_unique
    ON notification_outbox (notification_id, channel);

-- Drives the delivery/retry worker's claim query. Partial: terminal rows
-- are the overwhelming majority over time and must not be scanned.
CREATE INDEX notification_outbox_due_idx
    ON notification_outbox (next_attempt_at, id)
    WHERE status = 'pending';

-- Drives the DLQ reaper and the failure-rate alert.
CREATE INDEX notification_outbox_status_idx
    ON notification_outbox (tenant_id, status, updated_at DESC);

CREATE TRIGGER notification_outbox_set_updated_at
    BEFORE UPDATE ON notification_outbox
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE notification_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_outbox FORCE  ROW LEVEL SECURITY;

CREATE POLICY notification_outbox_tenant_select ON notification_outbox
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY notification_outbox_tenant_insert ON notification_outbox
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY notification_outbox_tenant_update ON notification_outbox
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
-- Delivery history is evidence. No app-level delete.
CREATE POLICY notification_outbox_tenant_delete ON notification_outbox
    FOR DELETE TO app_role
    USING (false);
CREATE POLICY notification_outbox_tenant_restrictive ON notification_outbox
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE ON notification_outbox TO app_role;

GRANT SELECT, INSERT, UPDATE ON notification_outbox TO app_notification_worker;
CREATE POLICY notification_outbox_worker_select ON notification_outbox
    FOR SELECT TO app_notification_worker
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY notification_outbox_worker_insert ON notification_outbox
    FOR INSERT TO app_notification_worker
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY notification_outbox_worker_update ON notification_outbox
    FOR UPDATE TO app_notification_worker
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY notification_outbox_worker_restrictive ON notification_outbox
    AS RESTRICTIVE FOR ALL TO app_notification_worker
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
