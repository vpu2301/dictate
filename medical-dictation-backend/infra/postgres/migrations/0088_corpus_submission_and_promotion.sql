-- 0088: dictated/authored candidate submission + accepted-candidate
-- promotion over HTTP (closes the sprint-21 ingest gap: the fill worksheet
-- could draft phrases but no HTTP route could create a corpus candidate,
-- and promotion existed only in the corpus-forge CLI as tenant_writer).
--
-- Two narrow SECURITY DEFINER functions — the 0085 pattern — because the
-- corpus pipeline operates on GLOBAL (tenant_id IS NULL) rows, which
-- app_role's RLS on corpus_candidates deliberately cannot INSERT or UPDATE:
--
--   corpus_submit_candidate()  — one authored phrase (typed or dictated in
--       the console fill worksheet) → a global 'candidate' row awaiting
--       review. Never creates anything already accepted; tier is fixed at 2
--       (machine-reviewable, human review always allowed); risk-flag
--       routing to tier 3 stays a reviewer/jury judgement. PII is rejected
--       at the HTTP boundary before this function is ever called.
--   corpus_promote_accepted()  — accepted global candidates → system-scope
--       autocomplete_phrases rows (mirrors corpus-forge promote_accepted;
--       idempotent via the 0039 unique index + ON CONFLICT DO NOTHING).
--       Release manifests (corpus_release stamp) remain a corpus-forge
--       operator action; promoted rows serve immediately because the trie
--       feeds review_state='accepted'.
--
-- Also adds corpus_candidates.submitted_by — attribution for HTTP
-- submissions (soft ref to users.sub, like reviewed_by).

BEGIN;

ALTER TABLE corpus_candidates ADD COLUMN submitted_by uuid;

COMMENT ON COLUMN corpus_candidates.submitted_by IS
    'users.sub of the console submitter (authored candidates); NULL for CLI-produced rows';

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

    -- SQL approximation of corpus_forge.domain.normalize.dedupe_key:
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
         2, '{}', p_submitted_by)
    RETURNING id INTO out_id;
    out_status := 'created';
END;
$$;

REVOKE ALL ON FUNCTION corpus_submit_candidate(text, text, text, text, text, uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION corpus_submit_candidate(text, text, text, text, text, uuid, uuid) TO app_role;
GRANT EXECUTE ON FUNCTION corpus_submit_candidate(text, text, text, text, text, uuid, uuid) TO tenant_writer;

CREATE OR REPLACE FUNCTION corpus_promote_accepted(
    p_candidate_ids uuid[] DEFAULT NULL,
    OUT out_promoted integer,
    OUT out_inserted integer
)
RETURNS record
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
    -- Same column mapping as corpus-forge promote_accepted; both paths are
    -- idempotent against each other via the 0039 unique index.
    WITH accepted AS (
        SELECT id, phrase, language, specialty, section_hint,
               source_kind, source_ref, tier, risk_flags,
               reviewed_by, reviewed_at, review_engine
        FROM corpus_candidates
        WHERE review_state = 'accepted' AND tenant_id IS NULL
          AND (p_candidate_ids IS NULL OR id = ANY (p_candidate_ids))
    ),
    ins AS (
        INSERT INTO autocomplete_phrases
            (tenant_id, owner_user_id, phrase, language, specialty,
             section_hint, source, source_kind, source_ref, tier,
             review_state, reviewed_by, reviewed_at, review_engine,
             risk_flags)
        SELECT NULL, NULL, phrase, language, specialty,
               section_hint, 'system', source_kind, source_ref, tier,
               'accepted', reviewed_by, reviewed_at, review_engine,
               risk_flags
        FROM accepted
        ON CONFLICT DO NOTHING
        RETURNING id
    )
    SELECT count(*) INTO out_inserted FROM ins;

    WITH flip AS (
        UPDATE corpus_candidates
           SET review_state = 'promoted', updated_at = now()
         WHERE review_state = 'accepted' AND tenant_id IS NULL
           AND (p_candidate_ids IS NULL OR id = ANY (p_candidate_ids))
        RETURNING id
    )
    SELECT count(*) INTO out_promoted FROM flip;
END;
$$;

REVOKE ALL ON FUNCTION corpus_promote_accepted(uuid[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION corpus_promote_accepted(uuid[]) TO app_role;
GRANT EXECUTE ON FUNCTION corpus_promote_accepted(uuid[]) TO tenant_writer;

COMMIT;
