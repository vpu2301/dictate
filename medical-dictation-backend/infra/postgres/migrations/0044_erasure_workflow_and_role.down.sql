-- Down for 0044 — remove the erasure workflow + mdx_erasure privileges.
-- (The role itself lives in init.sql and is NOT dropped here.)

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'audio_files', 'transcription_jobs', 'dictation_sessions',
        'reports', 'report_versions', 'report_synthesis_jobs',
        'encounters', 'clinical_notes', 'patient_consents',
        'patient_anamnesis', 'signing_sessions', 'signed_envelopes'
    ] LOOP
        -- (report_versions' policies were created explicitly, but the
        -- DROP POLICY IF EXISTS naming matches — one loop covers all.)
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_erasure_select', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_erasure_delete', t);
    END LOOP;
END $$;

DROP POLICY IF EXISTS patients_erasure_select ON patients;
DROP POLICY IF EXISTS patients_erasure_update ON patients;
DROP POLICY IF EXISTS patient_privacy_requests_erasure_select ON patient_privacy_requests;
DROP POLICY IF EXISTS patient_privacy_requests_erasure_update ON patient_privacy_requests;

REVOKE ALL ON audio_files, transcription_jobs, dictation_sessions,
             reports, report_versions, report_synthesis_jobs,
             encounters, clinical_notes, patient_consents,
             patient_anamnesis, signing_sessions, signed_envelopes,
             patients, patient_privacy_requests
FROM mdx_erasure;

ALTER TABLE patient_privacy_requests DROP CONSTRAINT privacy_approved_has_review;
ALTER TABLE patient_privacy_requests DROP CONSTRAINT privacy_two_person;
ALTER TABLE patient_privacy_requests DROP CONSTRAINT privacy_status_check;

-- Reverse status remap (best-effort onto the 0031 value set).
UPDATE patient_privacy_requests SET status = CASE
    WHEN status = 'requested'                      THEN 'pending'
    WHEN status = 'review'                         THEN 'scheduled'
    WHEN status = 'approved'                       THEN 'scheduled'
    WHEN status = 'executing'                      THEN 'scheduled'
    WHEN status = 'rejected'                       THEN 'cancelled'
    WHEN status = 'failed'                         THEN 'cancelled'
    ELSE status END;

ALTER TABLE patient_privacy_requests ADD CONSTRAINT patient_privacy_requests_status_check
    CHECK (status IN ('pending', 'scheduled', 'completed', 'cancelled'));

ALTER TABLE patient_privacy_requests
    DROP COLUMN reviewed_by,
    DROP COLUMN reviewed_at,
    DROP COLUMN rejection_reason,
    DROP COLUMN executing_at,
    DROP COLUMN completed_at,
    DROP COLUMN report_of_execution,
    DROP COLUMN package_object_key,
    DROP COLUMN package_deleted_at,
    DROP COLUMN last_error;
