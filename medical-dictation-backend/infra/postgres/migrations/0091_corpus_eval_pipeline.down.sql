BEGIN;

-- Children first: run items and snapshot items hang off their parents by FK.
DROP TABLE IF EXISTS corpus_eval_run_items;
DROP TABLE IF EXISTS corpus_eval_runs;
DROP TABLE IF EXISTS corpus_eval_snapshot_items;
DROP TABLE IF EXISTS corpus_eval_snapshots;
DROP TABLE IF EXISTS corpus_eval_script_items;

COMMIT;
