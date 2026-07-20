-- Sprint 12 / Day 7 — forensic record for deliveries that exhausted
-- their retries, and for envelopes that could not be processed at all.
--
-- Lives in the `audit` schema because it is evidence, not application
-- state: nothing in the request path reads it, and it must survive the
-- deletion of whatever it refers to.
--
-- Unlike the other audit-schema scratch tables, this one is NOT RLS-
-- exempt. It carries a tenant dimension whenever the envelope parsed, so
-- it can be guarded like everything else and no new entry is needed in
-- the `check-rls` exemption list.
--
-- `tenant_id` is NULLABLE for exactly one case: an envelope so malformed
-- that the tenant could not be read off it. Discarding that row would
-- destroy the only evidence of a broken producer, so it is written with
-- a NULL tenant and is consequently invisible to every tenant-scoped
-- role — only an operator connecting as a superuser sees it. The
-- policies below spell that out rather than leaving it implicit.

CREATE TABLE audit.notification_dead_letters (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- NULL only when the envelope could not be parsed. See above.
    tenant_id           UUID,

    -- Which axis died: 'ingest' (the envelope never materialised) or
    -- 'delivery' (the notification exists but a channel never sent).
    source              TEXT NOT NULL
                            CHECK (source IN ('ingest', 'delivery')),

    -- Set for source='delivery'. Deliberately NOT an FK: the whole point
    -- is to outlive the row it describes.
    notification_id     UUID,
    outbox_id           UUID,
    channel             TEXT NOT NULL DEFAULT ''
                            CHECK (channel IN ('', 'in_app', 'email')),

    -- Set for source='ingest': the producer's idempotency seed, so a
    -- forensic replay can be de-duplicated against what did materialise.
    event_id            UUID,

    attempt_count       INT NOT NULL DEFAULT 0,
    last_error          TEXT NOT NULL DEFAULT '',

    -- The full envelope, verbatim, for replay. PHI-free by the same
    -- construction as the envelope itself (scalar-only payload, enforced
    -- in libs/notification_events) — this table is not an exception to
    -- the PHI boundary, it inherits it.
    envelope            JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Drives the "DLQ non-empty" alert and the operator's triage view.
CREATE INDEX notification_dead_letters_recent_idx
    ON audit.notification_dead_letters (created_at DESC);
CREATE INDEX notification_dead_letters_tenant_idx
    ON audit.notification_dead_letters (tenant_id, created_at DESC)
    WHERE tenant_id IS NOT NULL;

ALTER TABLE audit.notification_dead_letters ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit.notification_dead_letters FORCE  ROW LEVEL SECURITY;

-- Tenant-scoped read for support tooling. NULL-tenant rows match no
-- tenant and are therefore operator-only, by design.
CREATE POLICY notification_dead_letters_tenant_select
    ON audit.notification_dead_letters
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
-- The NULL-tenant case is the unparseable envelope: it is written on a
-- pool connection with no tenant scope set, so the predicate must admit
-- it or the evidence is lost.
CREATE POLICY notification_dead_letters_tenant_insert
    ON audit.notification_dead_letters
    FOR INSERT TO app_role
    WITH CHECK (
        tenant_id IS NULL
        OR tenant_id = current_setting('app.tenant_id', true)::uuid
    );
CREATE POLICY notification_dead_letters_tenant_restrictive
    ON audit.notification_dead_letters
    AS RESTRICTIVE FOR ALL TO app_role
    USING (
        tenant_id IS NULL
        OR tenant_id = current_setting('app.tenant_id', true)::uuid
    );

-- app_role writes here too: the ingest consumer and the delivery worker
-- both run inside notification-service on the app_role pool, and a
-- dead-letter that cannot be written is evidence destroyed at exactly
-- the moment it is needed. Same reasoning as 0049 — the dedicated
-- worker role is for the future deploy split.
--
-- INSERT only. Nothing may UPDATE or DELETE a forensic row.
-- Table grants are inert without schema USAGE; app_role does not
-- otherwise reach into `audit` (audit.events is the audit_writer role's
-- alone), so it has to be granted explicitly here.
GRANT USAGE ON SCHEMA audit TO app_role;
GRANT SELECT, INSERT ON audit.notification_dead_letters TO app_role;
GRANT USAGE ON SCHEMA audit TO app_notification_worker;
GRANT SELECT, INSERT ON audit.notification_dead_letters TO app_notification_worker;

-- The worker may write a NULL-tenant row — that is the unparseable-
-- envelope case, and refusing it would lose the evidence.
CREATE POLICY notification_dead_letters_worker_select
    ON audit.notification_dead_letters
    FOR SELECT TO app_notification_worker
    USING (
        tenant_id IS NULL
        OR tenant_id = current_setting('app.tenant_id', true)::uuid
    );
CREATE POLICY notification_dead_letters_worker_insert
    ON audit.notification_dead_letters
    FOR INSERT TO app_notification_worker
    WITH CHECK (
        tenant_id IS NULL
        OR tenant_id = current_setting('app.tenant_id', true)::uuid
    );
CREATE POLICY notification_dead_letters_worker_restrictive
    ON audit.notification_dead_letters
    AS RESTRICTIVE FOR ALL TO app_notification_worker
    USING (
        tenant_id IS NULL
        OR tenant_id = current_setting('app.tenant_id', true)::uuid
    );
