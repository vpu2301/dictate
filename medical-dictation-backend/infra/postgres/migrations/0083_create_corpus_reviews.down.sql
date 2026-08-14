BEGIN;

DROP TABLE IF EXISTS corpus_reviews;
DROP FUNCTION IF EXISTS corpus_reviews_chain();
DROP FUNCTION IF EXISTS corpus_reviews_immutable();

COMMIT;
