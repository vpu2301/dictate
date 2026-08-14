BEGIN;

DROP FUNCTION IF EXISTS corpus_record_review(uuid, uuid, text, text, integer, text);

DROP INDEX IF EXISTS corpus_reviews_audit_rejections_idx;
ALTER TABLE corpus_reviews DROP COLUMN IF EXISTS mode;

UPDATE autocomplete_phrases
   SET tier = NULL,
       corpus_release = NULLIF(corpus_release, 'v0')
 WHERE source_kind = 'seed';

COMMIT;
