-- 0069_evidence_eval.sql
-- EVA-S03: retrieval eval infrastructure (the ADR-0019 standing-gate table
-- pattern, evidence-domain metrics).
--
-- DOCUMENTED EXCEPTION: these tables are NOT tenant-scoped and carry no RLS.
-- They hold eval infrastructure (curated questions, judgments, run metrics) —
-- never patient data, never tenant content beyond chunk references into the
-- GLOBAL corpus. They live in their own schema (evidence_eval), outside the
-- check-rls scan scope (['public','audit']), with grants restricted to
-- app_role (the eval harness identity). Widening access = ADR.

CREATE SCHEMA evidence_eval;
GRANT USAGE ON SCHEMA evidence_eval TO app_role;

CREATE TABLE evidence_eval.questions (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    text        text NOT NULL,
    lang        text NOT NULL CHECK (lang IN ('uk', 'en')),
    specialty   text,
    created_by  text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE evidence_eval.judgments (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    question_id     uuid NOT NULL REFERENCES evidence_eval.questions (id) ON DELETE CASCADE,
    -- Stable locator (canonical_id + section-path fragment) survives
    -- re-ingestion; chunk_id is resolved per corpus state at harness time.
    canonical_ref   text NOT NULL,
    chunk_id        uuid,
    grade           integer NOT NULL CHECK (grade BETWEEN 0 AND 3),
    judge           text NOT NULL,
    rubric_version  text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (question_id, canonical_ref, judge, rubric_version)
);

CREATE TABLE evidence_eval.runs (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind         text NOT NULL CHECK (kind IN ('retrieval')),
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    config       jsonb NOT NULL DEFAULT '{}',
    metrics      jsonb NOT NULL DEFAULT '{}',
    git_sha      text,
    model_pins   jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE evidence_eval.baseline (
    kind        text PRIMARY KEY CHECK (kind IN ('retrieval')),
    run_id      uuid NOT NULL REFERENCES evidence_eval.runs (id),
    adopted_at  timestamptz NOT NULL DEFAULT now(),
    adopted_by  text NOT NULL
);

GRANT SELECT, INSERT ON evidence_eval.questions, evidence_eval.judgments,
    evidence_eval.runs TO app_role;
GRANT SELECT, INSERT, UPDATE ON evidence_eval.baseline TO app_role;
GRANT UPDATE (finished_at, metrics) ON evidence_eval.runs TO app_role;
