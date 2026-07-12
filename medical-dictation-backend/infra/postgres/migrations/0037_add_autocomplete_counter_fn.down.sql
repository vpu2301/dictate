-- Reverse 0037: drop the counter-bump function. Counters already
-- accumulated on phrase rows are left as data.

DROP FUNCTION IF EXISTS autocomplete_bump_phrase_counters(uuid, bigint, bigint, timestamptz);
