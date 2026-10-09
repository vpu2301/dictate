-- Down for 0094. Dropping condition_confirmed loses which takes were
-- attested; re-applying restores the pre-Epic-C stance (everything true).
BEGIN;
DROP INDEX IF EXISTS corpus_eval_take_attempts_mismatch_idx;
ALTER TABLE corpus_eval_take_attempts
    DROP COLUMN IF EXISTS expected_condition,
    DROP COLUMN IF EXISTS condition_mismatch;
ALTER TABLE corpus_eval_takes DROP COLUMN IF EXISTS condition_confirmed;
DROP TABLE IF EXISTS corpus_eval_instruction_templates;
COMMIT;
