-- Reverse 0039: return the index to its (buggy) non-unique form.
-- Deduped rows are not restored.

DROP INDEX autocomplete_phrases_unique_phrase_per_owner;

CREATE INDEX autocomplete_phrases_unique_phrase_per_owner
    ON autocomplete_phrases (
        COALESCE(tenant_id,     '00000000-0000-0000-0000-000000000000'::uuid),
        COALESCE(owner_user_id, '00000000-0000-0000-0000-000000000000'::uuid),
        phrase,
        language
    );
