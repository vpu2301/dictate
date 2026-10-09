-- 0095: paired conditions — corpus-v3 Epic D.
--
-- THE DEFECT THE v2 MEASUREMENT EXPOSED. The per-condition table read
-- headset 20.8%, noisy 6.1%, phone 7.1% — the "worse" conditions scoring
-- better than the good one. Nothing about microphones explains that. The
-- explanation is that the three columns held different TEXTS: headset
-- carried the numeric and drug-name replicas, and the noisy column carried
-- three short baseline sentences. The table compared corpora and labelled
-- the result "conditions".
--
-- The fix is the oldest one in experiment design: record the SAME text in
-- both conditions and compare it against itself. `paired` marks the subset
-- of replicas that get recorded twice, and everything downstream stops
-- assuming one utterance is one recording.
--
-- WHY THIS TOUCHES THREE UNIQUE CONSTRAINTS. Every layer of the pipeline
-- was keyed on script_id alone, because until now a line HAD one take:
--
--   corpus_eval_takes         one take per line          → per (line, condition)
--   corpus_eval_snapshot_items one item per script_id    → per (script_id, condition)
--   corpus_eval_run_items      one score per script_id   → per (script_id, condition)
--
-- Leaving any one of them keyed on script_id would silently drop the second
-- recording somewhere between the microphone and the report — and the report
-- would still print a paired comparison, computed over half the data.
--
-- WHY run_items GAINS A condition COLUMN RATHER THAN JOINING FOR IT. The
-- scoring pump matches an in-flight ASR job back to the row that claimed it.
-- With two rows per script_id that match needs the condition, and re-deriving
-- it through a join to the snapshot on every tick is both slower and a place
-- for the two to disagree after a snapshot item is edited. The score records
-- which recording it scored.
--
-- BACKFILL. Existing run items take their condition from the snapshot item
-- they scored. A run item whose snapshot item has since gone (a deleted line)
-- has nothing to inherit, so it gets 'headset' — the pre-Epic-D default the
-- recorder actually used — rather than blocking the migration on a row whose
-- answer nobody can recover.

BEGIN;

-- ── mark the replicas that get recorded twice ─────────────────────────

ALTER TABLE corpus_eval_script_items
    -- Epic D recommends ~15 replicas, 2–3 per category. Not a property of
    -- the text — a decision about how this line is used in the design.
    ADD COLUMN paired boolean NOT NULL DEFAULT false;

CREATE INDEX corpus_eval_script_items_paired_idx
    ON corpus_eval_script_items (tenant_id)
    WHERE paired;

-- ── one take per (line, condition) ────────────────────────────────────

ALTER TABLE corpus_eval_takes
    DROP CONSTRAINT eval_take_one_per_line;
ALTER TABLE corpus_eval_takes
    ADD CONSTRAINT eval_take_one_per_line_condition
        UNIQUE (tenant_id, script_id, condition);

-- ── a snapshot holds every recording of every line ────────────────────

ALTER TABLE corpus_eval_snapshot_items
    DROP CONSTRAINT eval_snapshot_item_unique;
ALTER TABLE corpus_eval_snapshot_items
    ADD CONSTRAINT eval_snapshot_item_unique
        UNIQUE (snapshot_id, script_id, condition),
    -- Copied at publish time, like every other label here: whether this
    -- utterance was part of the paired design when the snapshot was frozen
    -- is a fact about the snapshot, not about the line as it stands today.
    ADD COLUMN paired boolean NOT NULL DEFAULT false;

-- ── a score names the recording it scored ─────────────────────────────

ALTER TABLE corpus_eval_run_items ADD COLUMN condition text;

UPDATE corpus_eval_run_items ri
   SET condition = si.condition
  FROM corpus_eval_runs r
  JOIN corpus_eval_snapshot_items si ON si.snapshot_id = r.snapshot_id
 WHERE ri.run_id = r.id AND si.script_id = ri.script_id;

UPDATE corpus_eval_run_items SET condition = 'headset' WHERE condition IS NULL;

ALTER TABLE corpus_eval_run_items
    ALTER COLUMN condition SET NOT NULL,
    ADD CONSTRAINT eval_run_item_condition_chk CHECK (
        condition IN ('headset', 'laptop-mic', 'phone-speaker-distance', 'noisy'));

ALTER TABLE corpus_eval_run_items
    DROP CONSTRAINT eval_run_item_unique;
ALTER TABLE corpus_eval_run_items
    ADD CONSTRAINT eval_run_item_unique UNIQUE (run_id, script_id, condition);

COMMIT;
