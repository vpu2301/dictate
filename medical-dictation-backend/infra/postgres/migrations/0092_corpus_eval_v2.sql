-- 0092: corpus eval v2 — dev/test sets, reproducible runs, append-only journals.
--
-- 0091 made a WER number producible from the console. Corpus-v2 §1.1 is the
-- list of reasons that number is not yet evidence:
--
--   · one raw WER, so "40 мг" vs "сорок міліграмів" scores as two errors
--     even though the two are clinically the same utterance;
--   · point estimates with no interval, on per-category samples of 4–7
--     utterances, where the honest CI is ±15–20 points;
--   · Whisper's silence hallucinations ("Дякую за перегляд!") averaged into
--     the corpus number instead of quarantined;
--   · nothing recorded about HOW a run was measured, so two runs a month
--     apart are not comparable even when the corpus is identical;
--   · one undivided corpus, so every tuning pass trains on the test set.
--
-- This migration adds the storage those five need. It adds no new score: the
-- arithmetic lives in eval_normalize/eval_stats/eval_flags, and this is the
-- shape it is written into.
--
-- WHY dataset IS A COLUMN AND NOT A CONVENTION. Holdout discipline that
-- exists only in a README is holdout discipline that survives until the
-- first deadline. `dataset` travels line → snapshot item → run, the run
-- carries the set it scored, and the comparison endpoint refuses two runs
-- that scored different sets. A dev number cannot become the official one by
-- being pasted into the wrong column.
--
-- WHY EXISTING ROWS BACKFILL TO 'test'. Everything authored before today IS
-- the v1 measurement, and v1 is what corpus-v2 §1.2 freezes as the holdout.
-- Defaulting them to 'dev' would silently make the frozen set tunable.
--
-- WHY THE JOURNALS ARE INSERT-ONLY. Both answer questions of the form "what
-- did we actually try?" — how many takes a line cost before one was kept,
-- which import added which lines. An UPDATE-able journal answers "what do we
-- currently believe we tried", which is a different and much less useful
-- thing. Neither table is granted UPDATE or DELETE; supersession is derived
-- on read (row_number over created_at), never written.

BEGIN;

-- ── dev/test sets ─────────────────────────────────────────────────────

ALTER TABLE corpus_eval_script_items
    ADD COLUMN dataset text NOT NULL DEFAULT 'dev'
        CONSTRAINT eval_item_dataset_chk CHECK (dataset IN ('dev', 'test'));

-- Pre-0092 authored lines are part of the v1 corpus, which is now the frozen
-- test set. Runs as `postgres` (the migrate runner), which bypasses RLS.
UPDATE corpus_eval_script_items SET dataset = 'test';

ALTER TABLE corpus_eval_snapshot_items
    ADD COLUMN dataset text NOT NULL DEFAULT 'dev'
        CONSTRAINT eval_snapshot_item_dataset_chk CHECK (dataset IN ('dev', 'test'));

UPDATE corpus_eval_snapshot_items SET dataset = 'test';

CREATE INDEX corpus_eval_snapshot_items_dataset_idx
    ON corpus_eval_snapshot_items (snapshot_id, dataset);

-- ── reproducible runs ─────────────────────────────────────────────────
--
-- corpus-v2 §1.3.5: "фіксація умов виміру … без цього виміри непорівнянні".
-- Every field here is something that changes the number without changing the
-- corpus.

ALTER TABLE corpus_eval_runs
    -- Which half of the corpus this run scored. A run measures one set;
    -- mixing dev and test into one average is the failure the split exists
    -- to prevent.
    ADD COLUMN dataset text NOT NULL DEFAULT 'test'
        CONSTRAINT eval_run_dataset_chk CHECK (dataset IN ('dev', 'test')),
    -- The normalisation rules the normalised WER was computed under. 'v0'
    -- means "raw only" — the honest label for every run that predates this
    -- migration, none of which had a normalised score at all.
    ADD COLUMN normalizer_version text NOT NULL DEFAULT 'v0',
    -- The snapshot manifest digest, copied at run start. The snapshot is
    -- immutable, so this is belt-and-braces — but a stored score whose
    -- corpus identity is a foreign key is a score that stops being checkable
    -- the moment the row is exported anywhere.
    ADD COLUMN corpus_sha256 text
        CONSTRAINT eval_run_corpus_sha_chk
        CHECK (corpus_sha256 IS NULL OR corpus_sha256 ~ '^[0-9a-f]{64}$'),
    -- Decode conditions AS REPORTED BY asr-service, not as configured here:
    -- {beam_size, temperature, language_hints, prompt_ids, model_revision,
    --  nlp_pipeline_version}. Anything the engine does not report is null
    -- rather than guessed — `temperature` is null today because no ASR
    -- surface in this system exposes it, and a plausible default written
    -- into an evidence field is worse than a gap.
    ADD COLUMN engine jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- Bootstrap resampling is pseudo-random; a fixed seed is what makes
    -- "two runs on identical data give identical numbers" (§4 P0-4) true of
    -- the confidence intervals and not just the point estimates.
    ADD COLUMN bootstrap_seed integer NOT NULL DEFAULT 20260814;

CREATE INDEX corpus_eval_runs_dataset_idx
    ON corpus_eval_runs (tenant_id, dataset, started_at DESC);

ALTER TABLE corpus_eval_run_items
    -- The normalised twin of wer/cer, plus its own reference length: the
    -- normaliser rewrites token counts ("сорок міліграмів" → "40 mg"), so
    -- weighting the normalised corpus average by the RAW word count would
    -- quietly weight every numeric line wrong.
    ADD COLUMN wer_norm double precision
        CHECK (wer_norm IS NULL OR wer_norm >= 0),
    ADD COLUMN cer_norm double precision
        CHECK (cer_norm IS NULL OR cer_norm >= 0),
    ADD COLUMN ref_words_norm integer
        CHECK (ref_words_norm IS NULL OR ref_words_norm >= 0),
    ADD COLUMN ref_chars_norm integer
        CHECK (ref_chars_norm IS NULL OR ref_chars_norm >= 0),
    -- Dose accuracy (§1.3.2): how many numeric tokens the reference carries,
    -- and whether the hypothesis got ALL of them. Not an average of partial
    -- credit — a dose is right or it is a different dose.
    ADD COLUMN dose_tokens integer
        CHECK (dose_tokens IS NULL OR dose_tokens >= 0),
    ADD COLUMN dose_exact boolean,
    -- Hallucination/quality flags, e.g. ["wer_over_100", "known_hallucination"].
    -- A flagged item keeps its score — it is just not allowed into the
    -- averages, and the console lists it separately so it gets re-recorded
    -- instead of silently dragging the corpus number.
    ADD COLUMN flags jsonb NOT NULL DEFAULT '[]'::jsonb,
    -- vad_seconds_speech from the ASR metadata, in ms. The input to the
    -- "<1 s of speech" and ">50% silence" flags; null when the engine did
    -- not report it, in which case those two flags cannot fire and say so.
    ADD COLUMN speech_ms integer
        CHECK (speech_ms IS NULL OR speech_ms >= 0);

CREATE INDEX corpus_eval_run_items_flagged_idx
    ON corpus_eval_run_items (run_id)
    WHERE flags <> '[]'::jsonb;

-- ── import journal (§6) ───────────────────────────────────────────────

CREATE TABLE corpus_eval_imports (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    filename     text        NOT NULL CHECK (char_length(filename) BETWEEN 1 AND 255),
    -- Digest of the uploaded bytes. Re-importing the same file is meant to
    -- be idempotent (§6); this is how the journal shows that it was.
    file_sha256  text        NOT NULL CHECK (file_sha256 ~ '^[0-9a-f]{64}$'),
    -- A dry run is journalled too: "we previewed this and did not commit it"
    -- is a fact worth keeping when the numbers later disagree.
    dry_run      boolean     NOT NULL,
    rows_total   integer     NOT NULL CHECK (rows_total >= 0),
    rows_added   integer     NOT NULL CHECK (rows_added >= 0),
    rows_skipped integer     NOT NULL CHECK (rows_skipped >= 0),
    rows_rejected integer    NOT NULL CHECK (rows_rejected >= 0),
    -- Per-row outcome with reasons, plus the post-import coverage matrix.
    report       jsonb       NOT NULL,
    imported_by  uuid        NOT NULL,   -- users.sub, soft ref (0089 pattern)
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX corpus_eval_imports_tenant_idx
    ON corpus_eval_imports (tenant_id, created_at DESC);

-- Insert-only: see the header. An edited import report is not a report.
GRANT SELECT, INSERT ON corpus_eval_imports TO app_role;
ALTER TABLE corpus_eval_imports ENABLE ROW LEVEL SECURITY;
ALTER TABLE corpus_eval_imports FORCE ROW LEVEL SECURITY;
CREATE POLICY eval_imports_tenant_all ON corpus_eval_imports
    FOR ALL TO app_role
    USING      (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- ── take-attempt journal (§7) ─────────────────────────────────────────
--
-- corpus_eval_takes keeps ONE take per line: re-recording replaces it, and
-- the replaced audio is gone. That is right for the corpus (one audio.wav
-- per utterance) and wrong for the question §7 asks — "скільки спроб коштує
-- репліка". This table is the answer: one row per attempt, kept or not.
--
-- It deliberately stores NO AUDIO. A discarded take's bytes are not evidence
-- of anything; its existence, duration and reason are.

CREATE TABLE corpus_eval_take_attempts (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    script_id    text        NOT NULL
        CONSTRAINT eval_attempt_script_id_chk
        CHECK (script_id ~ '^[a-z0-9][a-z0-9_-]{0,119}$'),
    -- The take this attempt produced, when it produced one. Soft reference:
    -- the take may be replaced or deleted later, and the attempt row must
    -- survive that — it is a record of what happened, not of what is.
    take_id      uuid,
    -- users.sub. THE SPEAKER, which is the variance component §1.2 wants
    -- decomposed: same line, different voices, different WER.
    speaker      uuid        NOT NULL,
    -- Free-text input-device label from the browser (`MediaDeviceInfo.label`),
    -- e.g. "AirPods Pro" — the other half of the same decomposition.
    device       text        CHECK (device IS NULL OR char_length(device) <= 120),
    condition    text        NOT NULL CHECK (
        condition IN ('headset', 'laptop-mic', 'phone-speaker-distance', 'noisy')),
    duration_ms  integer     NOT NULL CHECK (duration_ms >= 0),
    audio_sha256 text
        CHECK (audio_sha256 IS NULL OR audio_sha256 ~ '^[0-9a-f]{64}$'),
    -- saved     — stored as the line's current take
    -- discarded — the recorder threw it away before upload (re-take)
    -- rejected  — the server refused it (bad WAV, duration out of range)
    status       text        NOT NULL CHECK (status IN ('saved', 'discarded', 'rejected')),
    -- Why, for the two non-saved cases: 'retake', 'bad_duration', 'not_wav', …
    reason       text        CHECK (reason IS NULL OR char_length(reason) <= 64),
    -- clock_timestamp(), NOT now(): now() is the TRANSACTION's start time, so
    -- two attempts written in one transaction would share a timestamp and the
    -- ordering would fall through to a random uuid — which is exactly the
    -- ordering `superseded` is derived from. A journal that cannot say which
    -- attempt came second is not a journal.
    created_at   timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX corpus_eval_take_attempts_line_idx
    ON corpus_eval_take_attempts (tenant_id, script_id, created_at);

-- Insert-only. Supersession ("this attempt was later replaced") is a
-- row_number() over created_at at read time — deriving it cannot go stale,
-- and an UPDATE that rewrote history would defeat the point of the table.
GRANT SELECT, INSERT ON corpus_eval_take_attempts TO app_role;
ALTER TABLE corpus_eval_take_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE corpus_eval_take_attempts FORCE ROW LEVEL SECURITY;
CREATE POLICY eval_take_attempts_tenant_all ON corpus_eval_take_attempts
    FOR ALL TO app_role
    USING      (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

COMMIT;
