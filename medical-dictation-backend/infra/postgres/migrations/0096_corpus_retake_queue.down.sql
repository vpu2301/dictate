-- Down for 0096. The derived retake signals are unaffected; only the manual
-- "брак" marks are lost, since nothing else records them.
BEGIN;
DROP INDEX IF EXISTS corpus_eval_takes_flagged_idx;
ALTER TABLE corpus_eval_takes
    DROP CONSTRAINT IF EXISTS eval_take_flag_complete_chk,
    DROP COLUMN IF EXISTS flagged_at,
    DROP COLUMN IF EXISTS flagged_by,
    DROP COLUMN IF EXISTS flagged_note,
    DROP COLUMN IF EXISTS flagged_bad;
COMMIT;
