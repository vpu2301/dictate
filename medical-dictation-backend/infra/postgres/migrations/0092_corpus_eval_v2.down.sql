-- Down 0092. Drops the journals and the v2 columns.
--
-- LOSSY BY NATURE: the attempt journal and the import journal have no other
-- home, and the normalised scores cannot be recomputed from the raw ones.
-- Rolling this back returns the pipeline to point-estimate raw WER over one
-- undivided corpus.

BEGIN;

DROP TABLE IF EXISTS corpus_eval_take_attempts;
DROP TABLE IF EXISTS corpus_eval_imports;

DROP INDEX IF EXISTS corpus_eval_run_items_flagged_idx;
ALTER TABLE corpus_eval_run_items
    DROP COLUMN IF EXISTS wer_norm,
    DROP COLUMN IF EXISTS cer_norm,
    DROP COLUMN IF EXISTS ref_words_norm,
    DROP COLUMN IF EXISTS ref_chars_norm,
    DROP COLUMN IF EXISTS dose_tokens,
    DROP COLUMN IF EXISTS dose_exact,
    DROP COLUMN IF EXISTS flags,
    DROP COLUMN IF EXISTS speech_ms;

DROP INDEX IF EXISTS corpus_eval_runs_dataset_idx;
ALTER TABLE corpus_eval_runs
    DROP COLUMN IF EXISTS dataset,
    DROP COLUMN IF EXISTS normalizer_version,
    DROP COLUMN IF EXISTS corpus_sha256,
    DROP COLUMN IF EXISTS engine,
    DROP COLUMN IF EXISTS bootstrap_seed;

DROP INDEX IF EXISTS corpus_eval_snapshot_items_dataset_idx;
ALTER TABLE corpus_eval_snapshot_items DROP COLUMN IF EXISTS dataset;
ALTER TABLE corpus_eval_script_items DROP COLUMN IF EXISTS dataset;

COMMIT;
