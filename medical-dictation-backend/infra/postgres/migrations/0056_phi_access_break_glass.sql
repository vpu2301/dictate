-- S14 — admin ⟂ PHI separation, and the break-glass door through it.
--
-- The permission matrix (libs/auth/perms.py) now denies tenant_admin the
-- clinical surfaces: asr.*, dictation.*, report.read/write, note.*. An
-- administrator who genuinely needs ONE report — a patient complaint, a
-- legal request, a billing dispute — goes through break-glass instead of
-- holding a standing grant.
--
-- Two tables, because the flow has two independent halves:
--
--   auth_reauth_tickets  — PROOF OF PRESENCE. auth-service verifies the
--       admin's password against Keycloak and mints a single-use,
--       short-lived ticket. report-service consumes it. The password
--       itself never leaves auth-service, and only its sha256 lands
--       here, so a database read yields nothing replayable after the
--       ticket is consumed or expires.
--
--   phi_access_requests  — THE GRANT ITSELF. One row per break-glass
--       act: who, which report, which reason (closed vocabulary), the
--       free-text note, and the window it is valid for. The row is the
--       audit artefact; it is never deleted, only expired or revoked.
--
-- Deliberately NOT modelled: an approval step. This is a break-glass
-- door, not a request queue — the grant is immediate and the control is
-- after the fact (audit at `sec` severity + a notification to the
-- report's authors), because an admin facing a subpoena at 2am has
-- nobody to approve it. Revocation exists for when the review says the
-- reason did not hold up.

-- ── Step-up re-authentication tickets ───────────────────────────────

CREATE TABLE auth_reauth_tickets (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,

    -- The user who re-entered their password. The consumer checks this
    -- matches the caller's own `sub`, so a leaked ticket cannot be
    -- redeemed by anyone else.
    subject_sub     UUID NOT NULL REFERENCES users(sub) ON DELETE CASCADE,

    -- sha256 of the opaque ticket. The plaintext exists only in the
    -- response body and the SPA's memory.
    ticket_hash     BYTEA NOT NULL,

    -- What the ticket may be redeemed FOR. A step-up minted to open a
    -- report must not be replayable against some future high-risk
    -- action, so the consumer matches on this too.
    purpose         TEXT NOT NULL
                        CHECK (purpose IN ('phi_access_request')),

    issued_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    consumed_at     TIMESTAMPTZ
);

-- Global uniqueness (not per-tenant): the hash is 256 bits of CSPRNG
-- output, and a collision across tenants would be a redemption in the
-- wrong tenant.
CREATE UNIQUE INDEX auth_reauth_tickets_hash_unique
    ON auth_reauth_tickets (ticket_hash);
CREATE INDEX auth_reauth_tickets_expiry_idx
    ON auth_reauth_tickets (expires_at)
    WHERE consumed_at IS NULL;

ALTER TABLE auth_reauth_tickets ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth_reauth_tickets FORCE  ROW LEVEL SECURITY;

CREATE POLICY auth_reauth_tickets_tenant_select ON auth_reauth_tickets
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY auth_reauth_tickets_tenant_insert ON auth_reauth_tickets
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
-- UPDATE is the consumption stamp only; the WITH CHECK keeps a row from
-- being moved into another tenant on the way through.
CREATE POLICY auth_reauth_tickets_tenant_update ON auth_reauth_tickets
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
-- Spent and expired tickets are swept by the cleanup job; they carry no
-- PHI and no residual authority.
CREATE POLICY auth_reauth_tickets_tenant_delete ON auth_reauth_tickets
    FOR DELETE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY auth_reauth_tickets_tenant_restrictive ON auth_reauth_tickets
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON auth_reauth_tickets TO app_role;

-- ── Break-glass grants ──────────────────────────────────────────────

CREATE TABLE phi_access_requests (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,

    requested_by    UUID NOT NULL REFERENCES users(sub) ON DELETE RESTRICT,

    -- Only `report` today. A CHECK rather than a lookup table because
    -- widening it is a deliberate act that should show up in a diff:
    -- each new kind needs its own enforcement point in a service.
    resource_kind   TEXT NOT NULL DEFAULT 'report'
                        CHECK (resource_kind IN ('report')),
    resource_id     UUID NOT NULL,

    -- Denormalised from the report at request time so the oversight view
    -- can group by patient without re-reading the clinical tables (and
    -- so the trail survives the report being cancelled). Nullable: a
    -- report need not be linked to a patient.
    patient_id      UUID,

    -- Closed vocabulary. Free text alone is not a justification — a
    -- dropdown makes "why" reviewable in aggregate, and the note below
    -- carries the specifics.
    reason_code     TEXT NOT NULL CHECK (reason_code IN (
                        'patient_complaint',
                        'legal_request',
                        'billing_dispute',
                        'quality_review',
                        'care_continuity',
                        'data_correction',
                        'other'
                    )),
    reason_note     TEXT NOT NULL DEFAULT '',

    status          TEXT NOT NULL DEFAULT 'granted'
                        CHECK (status IN ('granted', 'revoked')),

    granted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,

    revoked_at      TIMESTAMPTZ,
    revoked_by      UUID REFERENCES users(sub) ON DELETE RESTRICT,

    -- Bumped on every read performed under this grant. Makes "requested
    -- access and never opened it" distinguishable from "read it eleven
    -- times", which is the question a compliance review actually asks.
    use_count       INTEGER NOT NULL DEFAULT 0,
    last_used_at    TIMESTAMPTZ,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- `other` is the escape hatch, so it must not be a way to skip the
-- justification: it requires the note the closed codes make optional.
ALTER TABLE phi_access_requests ADD CONSTRAINT phi_access_other_needs_note
    CHECK (reason_code <> 'other' OR length(btrim(reason_note)) >= 10);

ALTER TABLE phi_access_requests ADD CONSTRAINT phi_access_revoked_is_stamped
    CHECK ((status = 'revoked') = (revoked_at IS NOT NULL));

ALTER TABLE phi_access_requests ADD CONSTRAINT phi_access_window_is_forward
    CHECK (expires_at > granted_at);

-- The authorization lookup: "does this user hold a live grant on this
-- report right now". Partial on the live status so the index stays the
-- size of the open window, not of all history.
CREATE INDEX phi_access_requests_live_idx
    ON phi_access_requests (tenant_id, requested_by, resource_id, expires_at)
    WHERE status = 'granted';
-- The oversight list: most recent first.
CREATE INDEX phi_access_requests_recent_idx
    ON phi_access_requests (tenant_id, granted_at DESC, id DESC);

ALTER TABLE phi_access_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE phi_access_requests FORCE  ROW LEVEL SECURITY;

CREATE POLICY phi_access_requests_tenant_select ON phi_access_requests
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY phi_access_requests_tenant_insert ON phi_access_requests
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY phi_access_requests_tenant_update ON phi_access_requests
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
-- The grant log is the evidence that break-glass was used. Nothing
-- deletes it — expiry is a timestamp, not a DELETE.
CREATE POLICY phi_access_requests_tenant_delete ON phi_access_requests
    FOR DELETE TO app_role
    USING (false);
CREATE POLICY phi_access_requests_tenant_restrictive ON phi_access_requests
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE ON phi_access_requests TO app_role;

-- ── Notification category ───────────────────────────────────────────
-- `phi_access.granted` tells a report's authors that an administrator
-- opened it. See 0052 for why the CHECK must move in lockstep with the
-- `Category` enum: an un-admitted category becomes a CheckViolationError
-- inside the consumer, then a dead letter, and the clinician hears
-- nothing — which is precisely the failure this category exists to
-- prevent.

ALTER TABLE notifications
    DROP CONSTRAINT notifications_category_check;

ALTER TABLE notifications
    ADD CONSTRAINT notifications_category_check
        CHECK (category IN (
            'report.finalized',
            'report.signed',
            'report.signing_failed',
            'report.amended',
            'report.chain_failure',
            'report.shared_with_you',
            'dictation.completed',
            'transcription.completed',
            'transcription.failed',
            'phi_access.granted',
            'system.digest'
        ));

ALTER TABLE notification_preferences
    DROP CONSTRAINT notification_preferences_category_check;

ALTER TABLE notification_preferences
    ADD CONSTRAINT notification_preferences_category_check
        CHECK (category IN (
            'report.finalized',
            'report.signed',
            'report.signing_failed',
            'report.amended',
            'report.chain_failure',
            'report.shared_with_you',
            'dictation.completed',
            'transcription.completed',
            'transcription.failed',
            'phi_access.granted',
            'system.digest'
        ));
