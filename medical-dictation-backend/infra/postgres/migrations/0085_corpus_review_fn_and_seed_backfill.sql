-- 0085: corpus review via app_role + seed provenance completion (sprint 21
-- deployment slice).
--
-- (1) corpus_record_review(): the HTTP review surface (/corpus on
-- autocomplete-service, behind the corpus.review permission) runs as
-- app_role, whose RLS on corpus_candidates deliberately forbids UPDATEs to
-- global (tenant_id IS NULL) rows. Reviews on global candidates therefore
-- go through a narrow SECURITY DEFINER path — the 0036/0037/0040 pattern.
-- The function writes the append-only chained corpus_reviews row and the
-- candidate state transition in one transaction. Two modes:
--   mode='review' — only ever moves a row OUT of 'candidate'; cannot
--                   resurrect or re-decide.
--   mode='audit'  — the spot-audit path (plan §5.1): records a decision
--                   row AGAINST an already-'accepted' (jury-accepted)
--                   candidate WITHOUT changing its state. An audit
--                   rejection row is the jury-quality alert signal the
--                   dashboard reads; acting on it (retiring the phrase,
--                   re-queuing the jury version) stays an operator action.
--
-- (2) Seed backfill completion: the deployment plan stamps the sprint-10
-- starter rows tier=1 + corpus_release='v0' — reviewed once by the clinical
-- content lead in sprint 10; weak-but-real provenance, marked honestly.
-- (0081 set source_kind/source_ref/review_engine; this adds the rest.
-- COALESCE keeps any release stamp a dev environment already applied.)

BEGIN;

-- Discriminator for spot-audit decisions. Not part of the row_hash formula
-- (which predates it); immutability is trigger-enforced regardless.
ALTER TABLE corpus_reviews
    ADD COLUMN mode text NOT NULL DEFAULT 'review'
        CONSTRAINT reviews_mode_chk CHECK (mode IN ('review', 'audit'));

CREATE INDEX corpus_reviews_audit_rejections_idx
    ON corpus_reviews (decided_at DESC)
    WHERE mode = 'audit' AND decision = 'reject';

CREATE OR REPLACE FUNCTION corpus_record_review(
    p_candidate_id uuid,
    p_reviewer_id  uuid,
    p_decision     text,
    p_edited_text  text,
    p_latency_ms   integer,
    p_mode         text DEFAULT 'review'
) RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_new_state text;
    v_updated   integer;
BEGIN
    IF p_decision NOT IN ('accept', 'reject', 'edit') THEN
        RAISE EXCEPTION 'invalid decision: %', p_decision;
    END IF;
    IF p_mode NOT IN ('review', 'audit') THEN
        RAISE EXCEPTION 'invalid mode: %', p_mode;
    END IF;
    IF p_decision = 'edit' AND (p_edited_text IS NULL OR length(p_edited_text) = 0) THEN
        RAISE EXCEPTION 'edit decision requires edited_text';
    END IF;
    IF p_mode = 'audit' AND p_decision = 'edit' THEN
        RAISE EXCEPTION 'audit mode records accept/reject only';
    END IF;

    IF p_mode = 'audit' THEN
        -- Spot audit: the candidate must be machine-accepted; state is not
        -- changed — the review row itself is the signal.
        PERFORM 1 FROM corpus_candidates
         WHERE id = p_candidate_id
           AND review_state IN ('accepted', 'promoted')
           AND review_engine LIKE 'jury:%';
        IF NOT FOUND THEN
            RETURN NULL;  -- unknown id or not an auditable (jury-accepted) row
        END IF;
        INSERT INTO corpus_reviews
            (candidate_id, reviewer_id, decision, edited_text, latency_ms, mode)
        VALUES (p_candidate_id, p_reviewer_id, p_decision, NULL, p_latency_ms, 'audit');
        RETURN 'audited';
    END IF;

    v_new_state := CASE WHEN p_decision IN ('accept', 'edit') THEN 'accepted' ELSE 'rejected' END;

    -- State transition first: guarantees one decision per candidate even
    -- under concurrent reviewers (the WHERE clause is the lock).
    UPDATE corpus_candidates
       SET review_state = v_new_state,
           phrase        = COALESCE(p_edited_text, phrase),
           dedupe_key    = COALESCE(lower(btrim(p_edited_text)), dedupe_key),
           reviewed_by   = p_reviewer_id,
           reviewed_at   = now(),
           review_engine = 'human',
           updated_at    = now()
     WHERE id = p_candidate_id
       AND review_state = 'candidate';
    GET DIAGNOSTICS v_updated = ROW_COUNT;
    IF v_updated = 0 THEN
        RETURN NULL;  -- unknown id or already decided; caller maps to 404/409
    END IF;

    INSERT INTO corpus_reviews
        (candidate_id, reviewer_id, decision, edited_text, latency_ms, mode)
    VALUES (p_candidate_id, p_reviewer_id, p_decision, p_edited_text, p_latency_ms, 'review');

    RETURN v_new_state;
END;
$$;

REVOKE ALL ON FUNCTION corpus_record_review(uuid, uuid, text, text, integer, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION corpus_record_review(uuid, uuid, text, text, integer, text) TO app_role;
GRANT EXECUTE ON FUNCTION corpus_record_review(uuid, uuid, text, text, integer, text) TO tenant_writer;

-- ── Seed provenance completion ───────────────────────────────────────
UPDATE autocomplete_phrases
   SET tier           = COALESCE(tier, 1),
       corpus_release = COALESCE(corpus_release, 'v0')
 WHERE source_kind = 'seed';

COMMIT;
