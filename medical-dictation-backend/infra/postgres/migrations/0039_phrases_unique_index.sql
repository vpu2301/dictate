-- Sprint 10 fix — the per-scope phrase index was never actually UNIQUE.
--
-- 0023 created autocomplete_phrases_unique_phrase_per_owner as a plain
-- btree (the UNIQUE keyword was omitted; the snippets twin in 0024 has
-- it). Duplicate phrases per (scope, language) were silently accepted,
-- which skews ranking counters across the duplicate rows.
--
-- Dedupe first (keep the earliest row), then rebuild the index UNIQUE.

DELETE FROM autocomplete_phrases a
USING autocomplete_phrases b
WHERE a.created_at > b.created_at
  AND a.phrase = b.phrase
  AND a.language = b.language
  AND COALESCE(a.tenant_id,      '00000000-0000-0000-0000-000000000000'::uuid)
    = COALESCE(b.tenant_id,      '00000000-0000-0000-0000-000000000000'::uuid)
  AND COALESCE(a.owner_user_id,  '00000000-0000-0000-0000-000000000000'::uuid)
    = COALESCE(b.owner_user_id,  '00000000-0000-0000-0000-000000000000'::uuid);

DROP INDEX autocomplete_phrases_unique_phrase_per_owner;

CREATE UNIQUE INDEX autocomplete_phrases_unique_phrase_per_owner
    ON autocomplete_phrases (
        COALESCE(tenant_id,     '00000000-0000-0000-0000-000000000000'::uuid),
        COALESCE(owner_user_id, '00000000-0000-0000-0000-000000000000'::uuid),
        phrase,
        language
    );
