-- 0081: corpus provenance on autocomplete_phrases (sprint 21, ADR-0043).
--
-- Every phrase carries where it came from, who accepted it, and which
-- release it shipped in. Additive + backfill: existing rows (migration-0026
-- seeds and any API-authored rows) become source_kind='seed'/'authored',
-- review_state='accepted', so serving behaviour is unchanged the moment the
-- trie builder gains its `review_state = 'accepted'` predicate.
--
-- Tier semantics (ADR-0043): 1 = machine-acceptable after calibration,
-- 2 = jury-majority, 3 = human-mandatory. A row with risk_flags may only be
-- tier 3 — enforced here, not just in CI.

BEGIN;

ALTER TABLE autocomplete_phrases
    ADD COLUMN source_kind    text        NOT NULL DEFAULT 'authored',
    ADD COLUMN source_ref     text,
    ADD COLUMN tier           smallint,
    ADD COLUMN review_state   text        NOT NULL DEFAULT 'accepted',
    ADD COLUMN reviewed_by    uuid,               -- soft reference (users.sub), reports.patient_id precedent
    ADD COLUMN reviewed_at    timestamptz,
    ADD COLUMN review_engine  text,               -- 'human' | 'jury:<model>:<prompt_version>'
    ADD COLUMN corpus_release text,
    ADD COLUMN risk_flags     text[]      NOT NULL DEFAULT '{}';

ALTER TABLE autocomplete_phrases
    ADD CONSTRAINT phrases_source_kind_chk CHECK (
        source_kind IN ('mined', 'telemetry_gap', 'terminology', 'generated', 'authored', 'seed')),
    ADD CONSTRAINT phrases_review_state_chk CHECK (
        review_state IN ('candidate', 'accepted', 'rejected', 'retired')),
    ADD CONSTRAINT phrases_tier_chk CHECK (tier IS NULL OR tier BETWEEN 1 AND 3),
    -- Risk-flagged rows are human-mandatory: tier 3 or bust (ADR-0043 §6).
    ADD CONSTRAINT phrases_risk_tier_chk CHECK (
        cardinality(risk_flags) = 0 OR tier = 3);

-- Backfill: rows that predate this migration are the migration-0026 system
-- seeds (reviewed at authoring time by the clinical content path) plus any
-- clinician-authored user/tenant rows. Mark seeds explicitly.
UPDATE autocomplete_phrases
SET source_kind   = 'seed',
    source_ref    = 'seed:migration-0026',
    review_engine = 'human'
WHERE source = 'system';

-- Serving index: fetch_corpus() filters language + enabled + scope and now
-- review_state; partial index keeps the hot path tight.
CREATE INDEX autocomplete_phrases_serving_idx
    ON autocomplete_phrases (tenant_id, language, source, enabled)
    WHERE review_state = 'accepted';

-- Release stamping lookups.
CREATE INDEX autocomplete_phrases_release_idx
    ON autocomplete_phrases (corpus_release)
    WHERE corpus_release IS NOT NULL;

COMMIT;
