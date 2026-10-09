-- Down for 0097. Consent records are evidence; dropping the table discards
-- them, and the register can be rebuilt from snapshots and imports but the
-- consents cannot be rebuilt from anything. Re-apply only knowingly.
BEGIN;
DROP TABLE IF EXISTS dataset_registry;
DROP TABLE IF EXISTS corpus_speaker_consents;
COMMIT;
