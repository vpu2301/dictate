-- S11 steps 02+03 — close the last FK socket + make consent signable.
--
-- Step 02 half (below): `audio_files.encounter_id` has been a bare UUID
-- since 0007 ("nullable until sprint 11"). The erasure fan-out (step 05)
-- walks FK edges — a bare UUID column is an edge the database can't see
-- and CI can't guard. Promote it to a real FK so
-- recording → encounter → patient is a join, not a convention.
--
-- Step 03 half is appended by the consent-signing step (same coordinated
-- PR): `patient_consents.signed_envelope_id`.

-- ── Step 02: audio_files.encounter_id real FK ───────────────────────

-- Guard: fail loudly if orphans exist. There should be none in any real
-- environment; a polluted long-lived dev DB gets `make reset-db`. We
-- deliberately do NOT delete or NULL orphans here — silently dropping
-- the link is how PHI references get lost.
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM audio_files a
             WHERE a.encounter_id IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM encounters e
                               WHERE e.id = a.encounter_id))
  THEN RAISE EXCEPTION
      'orphan audio_files.encounter_id rows — resolve before migrating '
      '(dev: make reset-db; elsewhere: reattach or NULL the orphans '
      'deliberately, with an audit trail)';
  END IF;
END $$;

-- NULL stays legal (ad-hoc recordings); non-NULL must be a real
-- in-tenant encounter. RESTRICT: encounters are never hard-deleted
-- outside the erasure engine (step 07), which deletes recordings first.
ALTER TABLE audio_files
    ADD CONSTRAINT audio_files_encounter_fk
    FOREIGN KEY (encounter_id) REFERENCES encounters(id) ON DELETE RESTRICT;

-- Fan-out and timeline read path: "recordings of this encounter".
CREATE INDEX IF NOT EXISTS idx_audio_files_encounter
    ON audio_files (tenant_id, encounter_id)
    WHERE encounter_id IS NOT NULL;
