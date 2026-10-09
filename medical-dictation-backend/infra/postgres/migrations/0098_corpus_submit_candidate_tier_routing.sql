-- 0098: the HTTP ingest path must route tiers like every other ingest path.
--
-- THE BUG THIS CLOSES
--
-- 0088 gave the console a way to create a corpus candidate
-- (POST /corpus/candidates, the fill worksheet) and hard-coded its routing:
--
--     ... 'authored', ..., 2, '{}', p_submitted_by
--            source_kind        tier  risk_flags
--
-- Every phrase, whatever it said. A dose, a drug name, a laterality, a
-- negation, an ICD code — all of them filed as tier 2 with no risk flags,
-- while ADR-0043 §6 (and corpus_forge.domain.tiers.route_tier, and the
-- CHECK in 0082) say ANY risk flag is tier 3, human decision mandatory, no
-- exceptions. The CLI ingest paths have always honoured that; this one
-- silently did not.
--
-- The visible symptom was the review queue: it serves review_state
-- 'candidate' AND tier 3, so nothing submitted from the console could ever
-- appear in it. Operators authored phrases and watched a queue that stayed
-- empty for ever — the tier-1/2 backlog is reachable only from the pipeline
-- browser. The invisible symptom is the one that matters: a phrase carrying a
-- dose was one jury majority away from a clinician's cursor with no human
-- having read it.
--
-- THE FIX
--
-- The caller now supplies the routing, because the caller is the only place
-- the lexicon-backed flagger can run (autocomplete-service imports the shared
-- corpus_risk lib — the same module corpus-forge uses, deliberately not a
-- second implementation in SQL that would drift from it). This function stops
-- trusting it: the flag vocabulary is checked against 0082's CHECK, the tier
-- is checked against the routing table, and "flags ⇒ tier 3" is enforced here
-- as well as in the caller. A future ingest route that forgets to flag cannot
-- write a row this function will accept.
--
-- Tier 1 is rejected outright: route_tier only ever answers 1 for
-- source_kind='mined' with validators passed, and nothing authored in a
-- browser is mined.

BEGIN;

DROP FUNCTION IF EXISTS corpus_submit_candidate(text, text, text, text, text, uuid, uuid);

CREATE OR REPLACE FUNCTION corpus_submit_candidate(
    p_phrase       text,
    p_language     text,
    p_specialty    text,
    p_section_hint text,
    p_capture      text,
    p_submitted_by uuid,
    p_tenant_id    uuid,
    p_tier         int,
    p_risk_flags   text[],
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
    v_flags  text[] := coalesce(p_risk_flags, '{}');
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

    -- The routing contract, restated at the last gate before the row exists.
    IF NOT (v_flags <@ ARRAY['dose', 'drug', 'laterality', 'negation', 'icd', 'abbrev']::text[]) THEN
        RAISE EXCEPTION 'unknown risk flag in %', v_flags
            USING ERRCODE = 'check_violation';
    END IF;
    IF p_tier NOT IN (2, 3) THEN
        RAISE EXCEPTION 'authored candidates route to tier 2 or 3, got %', p_tier
            USING ERRCODE = 'check_violation';
    END IF;
    IF cardinality(v_flags) > 0 AND p_tier <> 3 THEN
        RAISE EXCEPTION 'risk-flagged candidate must be tier 3 (ADR-0043 §6), got tier %', p_tier
            USING ERRCODE = 'check_violation';
    END IF;

    v_phrase := regexp_replace(btrim(coalesce(p_phrase, '')), '\s+', ' ', 'g');
    IF v_phrase = '' OR char_length(v_phrase) > 80 THEN
        RAISE EXCEPTION 'phrase must be 1..80 characters'
            USING ERRCODE = 'check_violation';
    END IF;

    -- SQL approximation of corpus_risk.normalize.dedupe_key:
    -- lowercase, whitespace collapsed, apostrophes normalised to ’.
    v_key := lower(regexp_replace(v_phrase, '[''ʼ‘‛`]', '’', 'g'));

    -- Already serving in the global corpus → nothing to review.
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

    -- Same identity already in the pipeline (any state — a rejected
    -- candidate stays rejected; resubmission does not resurrect it).
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
         p_tier, v_flags, p_submitted_by)
    RETURNING id INTO out_id;
    out_status := 'created';
END;
$$;

REVOKE ALL ON FUNCTION corpus_submit_candidate(text, text, text, text, text, uuid, uuid, int, text[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION corpus_submit_candidate(text, text, text, text, text, uuid, uuid, int, text[]) TO app_role;
GRANT EXECUTE ON FUNCTION corpus_submit_candidate(text, text, text, text, text, uuid, uuid, int, text[]) TO tenant_writer;

-- Candidates written by the 0088 function carry the hard-coded tier 2 and an
-- empty flag array, and no SQL statement can re-derive the flags (the lexicon
-- lives in corpus_risk, not in the database). Rather than leave them looking
-- reviewed-by-machine-on-purpose, mark the ones still awaiting a decision so
-- an operator can re-flag them:
--
--   SELECT id, phrase FROM corpus_candidates
--    WHERE source_kind = 'authored' AND review_state = 'candidate'
--      AND source_ref LIKE 'authored:%' AND risk_flags = '{}';
--
-- The honest remedy for those rows is a human read — which is exactly what
-- the review queue is, and what they were kept out of. Decided rows
-- (accepted/rejected/promoted) already had their human, so nothing here
-- touches them.

COMMIT;
