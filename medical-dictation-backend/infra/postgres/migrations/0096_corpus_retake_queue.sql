-- 0096: the retake queue — corpus-v3 Epic E.
--
-- 0092 gave flagged takes a place to be listed. Epic E turns that list into
-- WORK: a filter in the recording queue, a counter, and a take that leaves
-- the filter the moment it is re-recorded.
--
-- ONLY ONE THING IS STORED HERE, and that is the point. Four signals put a
-- take on the retake list:
--
--   silence / hallucination   already computed per run (corpus_eval_run_items.flags)
--   condition_mismatch        already journalled per attempt (0094)
--   the human's "брак"        ← nothing records this, so it is what this
--                               migration adds
--
-- Deriving the first three at read time is what makes the queue self-clearing.
-- A stored "needs_retake" boolean would have to be recomputed, un-set on
-- re-record, and re-set when a later run flags the line again — three chances
-- for the queue to disagree with the evidence. Instead the queue asks: is
-- there a flag NEWER than this take's audio? Re-record and the answer becomes
-- no, for every derived signal at once, with nothing to remember to clear.
--
-- The manual mark is different in kind: it is a judgement, not an
-- observation, and no query can rederive "I listened to this and it is
-- unusable". So it is stored — and it is cleared by the take upsert, which
-- is the same self-clearing rule expressed the only way it can be for a fact
-- that lives in a person's head.
--
-- WHY THERE IS NO `superseded` COLUMN. Epic E asks that a replaced take stay
-- in the journal as superseded. It already does: corpus_eval_take_attempts
-- is insert-only and supersession is a row_number() over created_at (0092).
-- Adding a column would be a second, writable answer to a question that
-- already has a derived one.

BEGIN;

ALTER TABLE corpus_eval_takes
    -- The human's verdict on their own recording. Set from the recorder or
    -- the queue; CLEARED by the take upsert, because a new recording is a
    -- new take and inherits nothing from the one it replaced.
    ADD COLUMN flagged_bad boolean NOT NULL DEFAULT false,
    -- Free text, ≤200: "заїкнувся", "сусід почав свердлити". Kept short
    -- because it is a note to the next recordist, not a report.
    ADD COLUMN flagged_note text
        CHECK (flagged_note IS NULL OR char_length(flagged_note) <= 200),
    ADD COLUMN flagged_by uuid,          -- users.sub, soft ref (0089 pattern)
    ADD COLUMN flagged_at timestamptz,
    -- A flag with no timestamp is a flag nobody can date against the audio,
    -- which is the comparison the whole queue turns on.
    ADD CONSTRAINT eval_take_flag_complete_chk CHECK (
        (flagged_bad AND flagged_at IS NOT NULL) OR NOT flagged_bad);

CREATE INDEX corpus_eval_takes_flagged_idx
    ON corpus_eval_takes (tenant_id, script_id)
    WHERE flagged_bad;

COMMIT;
