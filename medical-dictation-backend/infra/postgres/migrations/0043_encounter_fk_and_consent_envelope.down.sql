-- Down for 0043 — reverse both halves in order (step 03 half first,
-- once it lands; then the step 02 FK half).

-- ── Step 02 half ────────────────────────────────────────────────────

DROP INDEX IF EXISTS idx_audio_files_encounter;
ALTER TABLE audio_files DROP CONSTRAINT audio_files_encounter_fk;
