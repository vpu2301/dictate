-- 0074 — Break-glass hotfix: relationship-era reason codes + step-up factors.
--
-- Two independent widenings, both additive, both required by the
-- treatment-relationship hotfix.
--
-- 1. REASON CODES. Break-glass used to be an admin-only act, and the
--    0056 vocabulary reads that way: complaint, subpoena, billing
--    dispute. Since the hotfix a CLINICIAN with no treatment
--    relationship to the patient goes through the same door — a covering
--    shift, a corridor consult, a patient who collapses in the waiting
--    room. None of the existing seven codes describe that honestly, and
--    a reviewer reading `other` for every clinical break-glass learns
--    nothing. Four codes are added for the clinical cases; all seven
--    existing codes stay valid, so live rows and the SPA dropdown are
--    unaffected.
--
-- 2. STEP-UP FACTORS. `auth_reauth_tickets` recorded THAT a step-up
--    happened but not WHAT was proven. With MFA now folded into the
--    step-up (TOTP required when the principal is enrolled, password
--    alone when they are not), "was this break-glass backed by a second
--    factor?" is a question a compliance review will ask, and the answer
--    has to be in the row rather than inferred from when MFA was rolled
--    out.

-- ── 1. Reason vocabulary ────────────────────────────────────────────

ALTER TABLE phi_access_requests
    DROP CONSTRAINT IF EXISTS phi_access_requests_reason_code_check;

ALTER TABLE phi_access_requests
    ADD CONSTRAINT phi_access_requests_reason_code_check
    CHECK (reason_code IN (
        -- Administrative cases (0056).
        'patient_complaint',
        'legal_request',
        'billing_dispute',
        'quality_review',
        'care_continuity',
        'data_correction',
        'other',
        -- Clinical cases (0074). A clinician reaching a patient who is
        -- not theirs.
        'emergency_care',
        'care_coordination',
        'patient_request',
        'technical_support'
    ));

-- ── 2. Step-up factors ──────────────────────────────────────────────

-- What the holder actually proved. Ordered set, lower-case, e.g.
-- {'password'} or {'password','totp'}. Defaulted to {'password'} because
-- that is exactly what every pre-0074 ticket proved — backfilling it as
-- a fact rather than leaving NULL keeps the oversight query total.
ALTER TABLE auth_reauth_tickets
    ADD COLUMN IF NOT EXISTS factors TEXT[] NOT NULL DEFAULT ARRAY['password'];

-- A ticket that proved nothing is not a step-up. The consumer trusts
-- this column, so an empty array must be impossible rather than merely
-- unlikely.
ALTER TABLE auth_reauth_tickets
    DROP CONSTRAINT IF EXISTS auth_reauth_tickets_factors_nonempty;
ALTER TABLE auth_reauth_tickets
    ADD CONSTRAINT auth_reauth_tickets_factors_nonempty
    CHECK (cardinality(factors) > 0);

-- Every recognised factor, so a typo in a service cannot write a value
-- the oversight view will silently fail to count.
ALTER TABLE auth_reauth_tickets
    DROP CONSTRAINT IF EXISTS auth_reauth_tickets_factors_known;
ALTER TABLE auth_reauth_tickets
    ADD CONSTRAINT auth_reauth_tickets_factors_known
    CHECK (factors <@ ARRAY['password', 'totp']);

-- Password is the floor: TOTP alone never mints a ticket, because the
-- code on a phone left on a desk proves less than the password does.
ALTER TABLE auth_reauth_tickets
    DROP CONSTRAINT IF EXISTS auth_reauth_tickets_factors_include_password;
ALTER TABLE auth_reauth_tickets
    ADD CONSTRAINT auth_reauth_tickets_factors_include_password
    CHECK ('password' = ANY (factors));

COMMENT ON COLUMN auth_reauth_tickets.factors IS
    'Authentication factors proven when this step-up ticket was minted. '
    'Always includes password; includes totp when the principal had MFA '
    'enrolled at step-up time (hotfix).';
