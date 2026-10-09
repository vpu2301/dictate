-- Down for 0095. Restoring the one-take-per-line keys REQUIRES that no line
-- actually holds two recordings, so the paired takes are dropped first,
-- newest kept. That is destructive and deliberate: a down-migration that
-- left the rows in place would fail on the unique constraint and leave the
-- schema half-reverted, which is worse.
BEGIN;

DELETE FROM corpus_eval_run_items ri
 USING (
     SELECT id, row_number() OVER (
                PARTITION BY run_id, script_id ORDER BY updated_at DESC, id
            ) AS rn
       FROM corpus_eval_run_items
 ) d
 WHERE d.id = ri.id AND d.rn > 1;

DELETE FROM corpus_eval_snapshot_items si
 USING (
     SELECT id, row_number() OVER (
                PARTITION BY snapshot_id, script_id ORDER BY id
            ) AS rn
       FROM corpus_eval_snapshot_items
 ) d
 WHERE d.id = si.id AND d.rn > 1;

DELETE FROM corpus_eval_takes t
 USING (
     SELECT id, row_number() OVER (
                PARTITION BY tenant_id, script_id ORDER BY updated_at DESC, id
            ) AS rn
       FROM corpus_eval_takes
 ) d
 WHERE d.id = t.id AND d.rn > 1;

ALTER TABLE corpus_eval_run_items
    DROP CONSTRAINT IF EXISTS eval_run_item_unique,
    DROP CONSTRAINT IF EXISTS eval_run_item_condition_chk,
    DROP COLUMN IF EXISTS condition;
ALTER TABLE corpus_eval_run_items
    ADD CONSTRAINT eval_run_item_unique UNIQUE (run_id, script_id);

ALTER TABLE corpus_eval_snapshot_items
    DROP CONSTRAINT IF EXISTS eval_snapshot_item_unique,
    DROP COLUMN IF EXISTS paired;
ALTER TABLE corpus_eval_snapshot_items
    ADD CONSTRAINT eval_snapshot_item_unique UNIQUE (snapshot_id, script_id);

ALTER TABLE corpus_eval_takes
    DROP CONSTRAINT IF EXISTS eval_take_one_per_line_condition;
ALTER TABLE corpus_eval_takes
    ADD CONSTRAINT eval_take_one_per_line UNIQUE (tenant_id, script_id);

DROP INDEX IF EXISTS corpus_eval_script_items_paired_idx;
ALTER TABLE corpus_eval_script_items DROP COLUMN IF EXISTS paired;

COMMIT;
