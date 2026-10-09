-- Restores the 0088 signature: the caller supplies no routing and every
-- authored candidate is filed tier 2 with no risk flags. Reverting this
-- migration re-opens the gap it closed — a dose phrase submitted from the
-- console will again bypass the human review queue — so the service must be
-- rolled back with it.

BEGIN;

DROP FUNCTION IF EXISTS corpus_submit_candidate(text, text, text, text, text, uuid, uuid, int, text[]);

CREATE OR REPLACE FUNCTION corpus_submit_candidate(
    p_phrase       text,
    p_language     text,
    p_specialty    text,
    p_section_hint text,
    p_capture      text,
    p_submitted_by uuid,
    p_tenant_id    uuid,
    OUT out_id     uuid,
    OUT out_status text
)
RETURNS record
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_phrase text;
    v_key    text;
BEGIN
    IF p_language NOT IN ('uk', 'en') THEN
        RAISE EXCEPTION 'invalid language: %', p_language;
    END IF;
    IF p_capture NOT IN ('typed', 'dictated') THEN
        RAISE EXCEPTION 'invalid capture: %', p_capture;
    END IF;
    IF p_submitted_by IS NULL THEN
        RAISE EXCEPTION 'submitted_by is required';
    END IF;

    v_phrase := regexp_replace(btrim(coalesce(p_phrase, '')), '\s+', ' ', 'g');
    IF v_phrase = '' OR char_length(v_phrase) > 80 THEN
        RAISE EXCEPTION 'phrase must be 1..80 characters'
            USING ERRCODE = 'check_violation';
    END IF;

    v_key := lower(regexp_replace(v_phrase, '[''ʼ‘‛`]', '’', 'g'));

    SELECT id INTO out_id
      FROM autocomplete_phrases
     WHERE source = 'system'
       AND language = p_language
       AND review_state = 'accepted'
       AND enabled = TRUE
       AND lower(regexp_replace(regexp_replace(btrim(phrase), '\s+', ' ', 'g'),
                                '[''ʼ‘‛`]', '’', 'g')) = v_key
     LIMIT 1;
    IF FOUND THEN
        out_status := 'already_in_corpus';
        RETURN;
    END IF;

    SELECT id INTO out_id
      FROM corpus_candidates
     WHERE tenant_id IS NULL
       AND language = p_language
       AND dedupe_key = v_key
     LIMIT 1;
    IF FOUND THEN
        out_status := 'duplicate_candidate';
        RETURN;
    END IF;

    INSERT INTO corpus_candidates
        (tenant_id, language, specialty, section_hint, phrase, dedupe_key,
         source_kind, source_ref, tier, risk_flags, submitted_by)
    VALUES
        (NULL, p_language,
         NULLIF(btrim(coalesce(p_specialty, '')), ''),
         NULLIF(btrim(coalesce(p_section_hint, '')), ''),
         v_phrase, v_key,
         'authored',
         'authored:' || p_capture || ':' || coalesce(p_tenant_id::text, 'unknown'),
         2, '{}', p_submitted_by)
    RETURNING id INTO out_id;
    out_status := 'created';
END;
$$;

REVOKE ALL ON FUNCTION corpus_submit_candidate(text, text, text, text, text, uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION corpus_submit_candidate(text, text, text, text, text, uuid, uuid) TO app_role;
GRANT EXECUTE ON FUNCTION corpus_submit_candidate(text, text, text, text, text, uuid, uuid) TO tenant_writer;

COMMIT;
