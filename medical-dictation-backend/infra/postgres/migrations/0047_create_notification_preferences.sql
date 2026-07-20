-- Sprint 12 / Day 2 — per-user delivery preferences.
--
-- Two tables, because the two things have different cardinality:
--   * `notification_preferences` — one row per (user, category) OVERRIDE.
--   * `notification_user_settings` — one row per user, for quiet hours
--     and timezone, which are not per-category.
--
-- ABSENT ROW MEANS "tenant default", deliberately. The defaults are NOT
-- expressed as column DEFAULTs and are NOT backfilled: they are resolved
-- in `domain/preferences.py` against `domain/catalog.py`. That way a
-- tenant admin can shift a category's default (sprint 17) and every user
-- who never expressed an opinion follows the new default immediately,
-- with no migration and no backfill. A backfilled row would freeze each
-- user at the default that happened to be current on the day they were
-- created.

CREATE TABLE notification_preferences (
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    user_id         UUID NOT NULL REFERENCES users(sub) ON DELETE CASCADE,

    category        TEXT NOT NULL
                        CHECK (category IN (
                            'report.finalized',
                            'report.signed',
                            'report.signing_failed',
                            'report.amended',
                            'report.chain_failure',
                            'report.shared_with_you',
                            'system.digest'
                        )),

    in_app_enabled  BOOLEAN NOT NULL DEFAULT TRUE,

    -- `email_mode` subsumes the on/off flag: 'off' IS disabled. A separate
    -- `email_enabled` boolean alongside a mode would allow the
    -- contradictory state (enabled=false, mode='immediate') and every
    -- reader would have to decide which wins. One column, no ambiguity
    -- (E8 — a preference bypass must be impossible by construction).
    email_mode      TEXT NOT NULL DEFAULT 'off'
                        CHECK (email_mode IN ('immediate', 'digest', 'off')),

    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, user_id, category)
);

CREATE INDEX notification_preferences_user_idx
    ON notification_preferences (tenant_id, user_id);

ALTER TABLE notification_preferences ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_preferences FORCE  ROW LEVEL SECURITY;

CREATE POLICY notification_preferences_tenant_select ON notification_preferences
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY notification_preferences_tenant_insert ON notification_preferences
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY notification_preferences_tenant_update ON notification_preferences
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY notification_preferences_tenant_delete ON notification_preferences
    FOR DELETE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY notification_preferences_tenant_restrictive ON notification_preferences
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON notification_preferences TO app_role;
-- The delivery worker READS preferences to decide suppression; it never
-- writes them.
GRANT SELECT ON notification_preferences TO app_notification_worker;
CREATE POLICY notification_preferences_worker_select ON notification_preferences
    FOR SELECT TO app_notification_worker
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY notification_preferences_worker_restrictive ON notification_preferences
    AS RESTRICTIVE FOR ALL TO app_notification_worker
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- ── notification_user_settings ──────────────────────────────────────
-- Quiet hours defer EMAIL only; in-app is never deferred (a badge is not
-- an interruption). Stored as local wall-clock TIME plus an IANA zone,
-- not as UTC offsets: an offset breaks twice a year at the DST boundary,
-- and "no email between 22:00 and 07:00 my time" must keep meaning that
-- through the transition (E9).

CREATE TABLE notification_user_settings (
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    user_id             UUID NOT NULL REFERENCES users(sub) ON DELETE CASCADE,

    -- NULL start/end = quiet hours disabled. Both must be set together;
    -- enforced by the CHECK below rather than by app code alone.
    quiet_hours_start   TIME,
    quiet_hours_end     TIME,

    -- IANA name, e.g. 'Europe/Kyiv'. Validated by the service against
    -- zoneinfo on write; stored as text because Postgres has no tz type.
    timezone            TEXT NOT NULL DEFAULT 'Europe/Kyiv',

    -- Local wall-clock hour at which the daily digest is sent.
    digest_hour         SMALLINT NOT NULL DEFAULT 8
                            CHECK (digest_hour BETWEEN 0 AND 23),

    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, user_id),

    CONSTRAINT notification_user_settings_quiet_hours_paired
        CHECK ((quiet_hours_start IS NULL) = (quiet_hours_end IS NULL))
);

ALTER TABLE notification_user_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_user_settings FORCE  ROW LEVEL SECURITY;

CREATE POLICY notification_user_settings_tenant_select ON notification_user_settings
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY notification_user_settings_tenant_insert ON notification_user_settings
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY notification_user_settings_tenant_update ON notification_user_settings
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY notification_user_settings_tenant_delete ON notification_user_settings
    FOR DELETE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY notification_user_settings_tenant_restrictive ON notification_user_settings
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON notification_user_settings TO app_role;
GRANT SELECT ON notification_user_settings TO app_notification_worker;
CREATE POLICY notification_user_settings_worker_select ON notification_user_settings
    FOR SELECT TO app_notification_worker
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY notification_user_settings_worker_restrictive ON notification_user_settings
    AS RESTRICTIVE FOR ALL TO app_notification_worker
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
