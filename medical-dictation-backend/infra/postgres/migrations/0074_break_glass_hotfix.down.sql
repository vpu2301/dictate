-- Down for 0074.
--
-- The reason-code narrowing is destructive by nature: any grant row
-- written with one of the four clinical codes violates the restored
-- 0056 constraint. Rather than delete break-glass history — which is the
-- one thing this table exists to keep — such rows are rewritten to
-- 'other' with the original code preserved in the note, so the audit
-- artefact survives the rollback in a readable form.

UPDATE phi_access_requests
   SET reason_note = btrim(
           'reason_code=' || reason_code || ' (retained through 0074 rollback). '
           || reason_note
       ),
       reason_code = 'other'
 WHERE reason_code IN (
        'emergency_care',
        'care_coordination',
        'patient_request',
        'technical_support'
       );

ALTER TABLE phi_access_requests
    DROP CONSTRAINT IF EXISTS phi_access_requests_reason_code_check;

ALTER TABLE phi_access_requests
    ADD CONSTRAINT phi_access_requests_reason_code_check
    CHECK (reason_code IN (
        'patient_complaint',
        'legal_request',
        'billing_dispute',
        'quality_review',
        'care_continuity',
        'data_correction',
        'other'
    ));

ALTER TABLE auth_reauth_tickets
    DROP CONSTRAINT IF EXISTS auth_reauth_tickets_factors_include_password;
ALTER TABLE auth_reauth_tickets
    DROP CONSTRAINT IF EXISTS auth_reauth_tickets_factors_known;
ALTER TABLE auth_reauth_tickets
    DROP CONSTRAINT IF EXISTS auth_reauth_tickets_factors_nonempty;
ALTER TABLE auth_reauth_tickets
    DROP COLUMN IF EXISTS factors;
