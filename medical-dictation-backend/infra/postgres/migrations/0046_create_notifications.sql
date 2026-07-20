-- Sprint 12 / Day 1 — `notifications`: the per-recipient record.
--
-- One domain fact fans out to zero or more rows here, one per recipient.
-- The row is the source of truth for the in-app feed and the badge count;
-- the WebSocket is a delivery optimisation, never the state (E5 — a
-- reconnecting client always re-reads the count from here).
--
-- `body_text` is PHI-FREE BY CONSTRUCTION and pre-rendered at
-- materialisation time. It carries pointers — a report code, a deep link
-- — never report content, patient name, or diagnosis (ADR-0031). The
-- column is plaintext because it is defined to hold no PHI; that is a
-- boundary enforced by the blocking CI gate
-- `scripts/ci/check-notification-phi-free.py`, not by encryption.
--
-- `dedupe_key` is the idempotency anchor. It is derived by the consumer
-- from the producer's `event_id` plus the recipient, so an at-least-once
-- redelivery of the same fact collapses onto the row already written
-- instead of notifying someone twice (E3).

CREATE TABLE notifications (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,

    -- The Keycloak principal who should see this. FK to users(sub) —
    -- the same convention the rest of the schema uses (batch-A fix).
    recipient_user_id   UUID NOT NULL REFERENCES users(sub) ON DELETE CASCADE,

    category            TEXT NOT NULL
                            CHECK (category IN (
                                'report.finalized',
                                'report.signed',
                                'report.signing_failed',
                                'report.amended',
                                'report.chain_failure',
                                'report.shared_with_you',
                                'system.digest'
                            )),

    -- UNIQUE per tenant — see the module comment above.
    dedupe_key          TEXT NOT NULL,

    title               TEXT NOT NULL,
    body_text           TEXT NOT NULL DEFAULT '',
    deep_link           TEXT NOT NULL DEFAULT '',

    -- The allow-listed, PHI-free projection the title/body were rendered
    -- from (`domain/render.safe_payload`). Stored so the EMAIL channel
    -- can re-render from the same pointers later, rather than scraping
    -- them back out of a localised title string — that parse would break
    -- the moment a template's word order changed.
    --
    -- This is not a hole in the PHI boundary: it holds exactly what the
    -- per-category allow-list admitted, and the blocking CI gate
    -- (`check-notification-phi-free.py`) renders against it.
    render_fields       JSONB NOT NULL DEFAULT '{}'::jsonb,

    resource_type       TEXT NOT NULL DEFAULT '',
    resource_id         UUID,

    severity            TEXT NOT NULL DEFAULT 'info'
                            CHECK (severity IN ('info', 'warning', 'critical')),

    read_at             TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The idempotency anchor. Scoped by tenant so two tenants replaying
-- unrelated events can never collide.
CREATE UNIQUE INDEX notifications_dedupe_key_unique
    ON notifications (tenant_id, dedupe_key);

-- Drives the unread-first feed. `read_at NULLS FIRST` puts unread rows
-- ahead of read ones without a second query or a UNION.
CREATE INDEX notifications_feed_idx
    ON notifications (recipient_user_id, read_at NULLS FIRST, created_at DESC);

-- Drives the badge count, which is polled far more often than the feed
-- is read. Partial so it stays small no matter how much history a
-- long-lived user accumulates.
CREATE INDEX notifications_unread_idx
    ON notifications (recipient_user_id)
    WHERE read_at IS NULL;

ALTER TABLE notifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE notifications FORCE  ROW LEVEL SECURITY;

-- PERMISSIVE: tenant visibility.
CREATE POLICY notifications_tenant_select ON notifications
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY notifications_tenant_insert ON notifications
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY notifications_tenant_update ON notifications
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
-- History is append-only from the app's perspective; expiry is a
-- retention job running as a privileged role, not a user action.
CREATE POLICY notifications_tenant_delete ON notifications
    FOR DELETE TO app_role
    USING (false);

-- RESTRICTIVE: the tenant predicate holds regardless of any policy added
-- later — defence in depth, same shape as `users`.
CREATE POLICY notifications_tenant_restrictive ON notifications
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE ON notifications TO app_role;

-- ── app_notification_worker ─────────────────────────────────────────
-- The delivery/digest workers get their own role. They must be able to
-- materialise rows and mark deliveries, but never to erase history — so
-- a leaked worker credential cannot destroy the audit trail of what was
-- sent to whom.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_notification_worker') THEN
        CREATE ROLE app_notification_worker NOLOGIN;
    END IF;
END
$$;

GRANT SELECT, INSERT, UPDATE ON notifications TO app_notification_worker;

CREATE POLICY notifications_worker_select ON notifications
    FOR SELECT TO app_notification_worker
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY notifications_worker_insert ON notifications
    FOR INSERT TO app_notification_worker
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY notifications_worker_update ON notifications
    FOR UPDATE TO app_notification_worker
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY notifications_worker_restrictive ON notifications
    AS RESTRICTIVE FOR ALL TO app_notification_worker
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
