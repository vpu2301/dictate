-- Sprint 07 / Day 6 — eval_runs + eval_utterances.
--
-- Stores nightly WER eval results. Lives in the `audit` schema (read-
-- mostly; never tenant-bearing). No RLS needed since these aren't
-- tenant rows, but we add a permissive policy for consistency.

CREATE TABLE audit.eval_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    corpus_version  TEXT NOT NULL,                  -- e.g. "v1"
    model           TEXT NOT NULL,                  -- "large-v3"
    pipeline_version TEXT NOT NULL,                 -- nlp pipeline version
    prompts_hash    TEXT NOT NULL,                  -- sha256 of prompts corpus
    utterances      INTEGER NOT NULL DEFAULT 0,
    wer_overall_uk  NUMERIC(6,4),
    wer_overall_en  NUMERIC(6,4),
    cer_overall_uk  NUMERIC(6,4),
    cer_overall_en  NUMERIC(6,4),
    rtf_p50         NUMERIC(6,3),
    rtf_p95         NUMERIC(6,3),
    notes           TEXT
);

CREATE INDEX eval_runs_started_idx
    ON audit.eval_runs (started_at DESC);

CREATE TABLE audit.eval_utterances (
    run_id          UUID NOT NULL REFERENCES audit.eval_runs(id) ON DELETE CASCADE,
    utterance_id    TEXT NOT NULL,
    language        TEXT NOT NULL CHECK (language IN ('uk','en')),
    specialty       TEXT NOT NULL,
    duration_s      NUMERIC(7,2) NOT NULL,
    wer             NUMERIC(6,4) NOT NULL,
    cer             NUMERIC(6,4) NOT NULL,
    rtf             NUMERIC(6,3) NOT NULL,
    number_norm_score NUMERIC(6,4),
    reference       TEXT NOT NULL,
    hypothesis      TEXT NOT NULL,
    PRIMARY KEY (run_id, utterance_id)
);

CREATE INDEX eval_utterances_lang_spec_idx
    ON audit.eval_utterances (language, specialty);

GRANT SELECT ON audit.eval_runs, audit.eval_utterances TO audit_reader;
GRANT INSERT, SELECT ON audit.eval_runs, audit.eval_utterances TO audit_writer;

ALTER TABLE audit.eval_runs        ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit.eval_utterances  ENABLE ROW LEVEL SECURITY;

CREATE POLICY eval_runs_select_all ON audit.eval_runs
    FOR SELECT TO audit_writer, audit_reader USING (true);
CREATE POLICY eval_runs_insert_writer ON audit.eval_runs
    FOR INSERT TO audit_writer WITH CHECK (true);

CREATE POLICY eval_utterances_select_all ON audit.eval_utterances
    FOR SELECT TO audit_writer, audit_reader USING (true);
CREATE POLICY eval_utterances_insert_writer ON audit.eval_utterances
    FOR INSERT TO audit_writer WITH CHECK (true);

-- ── Baseline table ──────────────────────────────────────────────────
-- Stores the rolling baseline WER (sprint-07 day-7 establishes from
-- first 3 nightly runs; subsequent runs alert if they regress beyond
-- baseline + 1 pp.
CREATE TABLE audit.eval_baseline (
    id              SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),  -- singleton
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    wer_overall_uk  NUMERIC(6,4) NOT NULL,
    wer_overall_en  NUMERIC(6,4) NOT NULL,
    rtf_p95         NUMERIC(6,3) NOT NULL,
    notes           TEXT
);

GRANT SELECT, INSERT, UPDATE ON audit.eval_baseline TO audit_writer;
GRANT SELECT ON audit.eval_baseline TO audit_reader;
