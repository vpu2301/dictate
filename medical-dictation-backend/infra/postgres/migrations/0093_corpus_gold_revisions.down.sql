-- Down for 0093. Dropping the table takes the vendored-spine seed with it;
-- the "еталон змінено" marking is derived from these rows, so it simply
-- stops appearing. Re-applying 0093 re-seeds it.
BEGIN;
DROP TABLE IF EXISTS corpus_eval_gold_revisions;
COMMIT;
