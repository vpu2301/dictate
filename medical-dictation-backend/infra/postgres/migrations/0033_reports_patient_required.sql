-- Sprint 11 follow-up — dictated reports must always reference a patient.
--
-- Migration 0016 shipped `reports.patient_id` as a nullable column with no
-- FK ("sprint 11 brings the patients table"). Migration 0031 created
-- `patients` but deliberately left the cross-service link a soft reference.
-- Now that the frontend forces a patient before the Dictation Studio mounts,
-- we promote the link to a real, enforced invariant (defence-in-depth):
--
--   1. Quarantine any legacy patient-less rows (there should be none in dev;
--      a patient-less report is not a valid clinical record, so we cancel
--      rather than block the migration).
--   2. Add the FK to `patients` that 0031 deferred.
--   3. Require a patient for every NON-cancelled report via a CHECK that
--      tolerates the already-cancelled legacy rows from step 1 (a plain
--      NOT NULL would retro-reject those quarantined rows).
--
-- The runner wraps each migration in its own transaction, so no explicit
-- BEGIN/COMMIT here (mirrors 0031/0032).

-- 1. Backfill / quarantine any legacy patient-less rows BEFORE the constraint.
UPDATE reports
   SET status = 'cancelled',
       cancelled_at = now(),
       cancelled_reason = 'migration_0033_missing_patient'
 WHERE patient_id IS NULL
   AND status <> 'cancelled';

-- 2. FK to patients (sprint-11 deferral, now that 0031 exists).
ALTER TABLE reports
    ADD CONSTRAINT reports_patient_fk
    FOREIGN KEY (patient_id) REFERENCES patients(id) ON DELETE RESTRICT;

-- 3. Require a patient for every NEW report, but don't retro-break the
--    already-cancelled legacy rows quarantined in step 1.
ALTER TABLE reports
    ADD CONSTRAINT reports_patient_required
    CHECK (patient_id IS NOT NULL OR status = 'cancelled');

-- Keep the partial index reports_patient_idx (0016) as-is.
