-- S21 — `mfa_reminders`: an oversight role asking a user to turn MFA on.
--
-- The access review (`GET /admin/users`, rendered at /audit/access) shows an
-- auditor who holds a second factor and who does not. Seeing it is half the
-- control; the other half is being able to do something about it without
-- holding any power over the account. An auditor may not invite, deactivate,
-- change roles or reset MFA — the whole point of the role is that it cannot
-- touch what it reviews — so the one action it gets is this: raise a standing
-- request that the user enrols.
--
-- STANDING is the word that shapes the table. A notification is a moment; a
-- finding stays open until it is closed. One row per (tenant, user) holds the
-- open request, and it closes exactly one way: the user enrols, and
-- `POST /auth/mfa/verify` stamps `resolved_at`. There is no dismiss button
-- anywhere in the product, because a reminder a user can wave away is a
-- reminder that measures nothing. Re-reminding the same user bumps
-- `reminder_count` and `last_reminded_at` on the SAME row rather than
-- inserting a second one — the count is the escalation record an auditor
-- shows when the finding is still open three reviews later.
--
-- The row is also read by the *subject*: `GET /auth/me` returns the open
-- reminder so the SPA can carry a banner until enrolment. That read crosses
-- no privilege boundary — a user learning that they were asked to secure
-- their own account is the entire intent.

CREATE TABLE mfa_reminders (
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,

    -- Who was asked. CASCADE: the reminder is about an account, and an
    -- account that no longer exists has no open finding.
    subject_sub     UUID NOT NULL REFERENCES users(sub) ON DELETE CASCADE,

    -- Who asked. Nullable + SET NULL rather than CASCADE: the reminder
    -- outlives the reviewer who raised it. Deleting the auditor must not
    -- silently close a finding on somebody else's account.
    requested_by    UUID REFERENCES users(sub) ON DELETE SET NULL,
    -- Denormalised so the subject's banner can say "your clinic
    -- administrator" vs "an auditor" without a join the subject is not
    -- allowed to make, and so it survives `requested_by` going NULL.
    requested_by_role TEXT NOT NULL
                        CHECK (requested_by_role IN ('tenant_admin', 'auditor')),

    first_reminded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_reminded_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    reminder_count    INTEGER NOT NULL DEFAULT 1 CHECK (reminder_count > 0),

    -- NULL ⇒ open. Stamped by MFA enrolment, never by the user dismissing
    -- anything. Kept (rather than the row deleted) so the review history
    -- reads "asked twice, enrolled on the 4th" instead of going blank.
    resolved_at       TIMESTAMPTZ,

    PRIMARY KEY (tenant_id, subject_sub)
);

-- The roster join reads open reminders for a page of users at a time.
CREATE INDEX mfa_reminders_open_idx
    ON mfa_reminders (tenant_id, subject_sub)
    WHERE resolved_at IS NULL;

GRANT SELECT, INSERT, UPDATE, DELETE ON mfa_reminders TO app_role, tenant_writer;

ALTER TABLE mfa_reminders ENABLE ROW LEVEL SECURITY;
ALTER TABLE mfa_reminders FORCE  ROW LEVEL SECURITY;

CREATE POLICY mfa_reminders_app_role_tenant ON mfa_reminders
    FOR ALL TO app_role
    USING      (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY mfa_reminders_writer_tenant ON mfa_reminders
    FOR ALL TO tenant_writer
    USING      (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- RESTRICTIVE: defence in depth, same shape as `users`.
CREATE POLICY mfa_reminders_tenant_restrictive ON mfa_reminders
    AS RESTRICTIVE FOR ALL
    USING      (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
