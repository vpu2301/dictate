BEGIN;

DROP FUNCTION IF EXISTS corpus_promote_accepted(uuid[]);
DROP FUNCTION IF EXISTS corpus_submit_candidate(text, text, text, text, text, uuid, uuid);

ALTER TABLE corpus_candidates DROP COLUMN IF EXISTS submitted_by;

COMMIT;
