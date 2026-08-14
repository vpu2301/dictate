BEGIN;

DROP INDEX IF EXISTS autocomplete_phrases_release_idx;
DROP INDEX IF EXISTS autocomplete_phrases_serving_idx;

ALTER TABLE autocomplete_phrases
    DROP CONSTRAINT IF EXISTS phrases_risk_tier_chk,
    DROP CONSTRAINT IF EXISTS phrases_tier_chk,
    DROP CONSTRAINT IF EXISTS phrases_review_state_chk,
    DROP CONSTRAINT IF EXISTS phrases_source_kind_chk;

ALTER TABLE autocomplete_phrases
    DROP COLUMN IF EXISTS risk_flags,
    DROP COLUMN IF EXISTS corpus_release,
    DROP COLUMN IF EXISTS review_engine,
    DROP COLUMN IF EXISTS reviewed_at,
    DROP COLUMN IF EXISTS reviewed_by,
    DROP COLUMN IF EXISTS review_state,
    DROP COLUMN IF EXISTS tier,
    DROP COLUMN IF EXISTS source_ref,
    DROP COLUMN IF EXISTS source_kind;

COMMIT;
