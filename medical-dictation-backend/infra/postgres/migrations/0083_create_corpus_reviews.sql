-- 0083: corpus_reviews — append-only, hash-chained review decisions
-- (sprint 21, ADR-0043 §4).
--
-- Two purposes: the DPO's audit trail for every human decision over the
-- corpus, and the latency instrument (latency_ms) that tells us by day 7
-- whether review really costs 15 s/decision or secretly 90 s.
--
-- Chain: single global chain (reviews are a low-volume operator stream, not
-- per-tenant clinical traffic), computed server-side in the BEFORE INSERT
-- trigger with pgcrypto digest() — unlike audit.events, whose chain is
-- app-side in AuditWriter, reviews may be written by more than one tool
-- (corpus-forge CLI, a future review UI), so the DB is the one place the
-- chain can't be skipped. Serialised via advisory xact lock.

BEGIN;

CREATE TABLE corpus_reviews (
    seq          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    candidate_id uuid        NOT NULL REFERENCES corpus_candidates(id),
    reviewer_id  uuid        NOT NULL,      -- users.sub (soft reference)
    decision     text        NOT NULL CHECK (decision IN ('accept', 'reject', 'edit')),
    edited_text  text        CHECK (edited_text IS NULL OR char_length(edited_text) BETWEEN 1 AND 80),
    decided_at   timestamptz NOT NULL DEFAULT now(),
    latency_ms   integer     CHECK (latency_ms IS NULL OR latency_ms >= 0),
    prev_hash    bytea,                     -- NULL only for seq=1
    row_hash     bytea       NOT NULL,
    CONSTRAINT edit_has_text CHECK (decision <> 'edit' OR edited_text IS NOT NULL)
);

CREATE INDEX corpus_reviews_candidate_idx ON corpus_reviews (candidate_id);
CREATE INDEX corpus_reviews_reviewer_idx ON corpus_reviews (reviewer_id, decided_at DESC);

CREATE OR REPLACE FUNCTION corpus_reviews_chain() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    last_hash bytea;
BEGIN
    -- Serialise chain computation; lock key is arbitrary but unique to this table.
    PERFORM pg_advisory_xact_lock(hashtext('corpus_reviews_chain'));

    SELECT row_hash INTO last_hash FROM corpus_reviews ORDER BY seq DESC LIMIT 1;

    NEW.prev_hash := last_hash;  -- NULL for the genesis row
    NEW.row_hash := digest(
        COALESCE(last_hash, '\x0000000000000000000000000000000000000000000000000000000000000000'::bytea)
        || convert_to(
            NEW.candidate_id::text || '|' || NEW.reviewer_id::text || '|' ||
            NEW.decision || '|' || COALESCE(NEW.edited_text, '') || '|' ||
            to_char(NEW.decided_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
            'UTF8'),
        'sha256');
    RETURN NEW;
END;
$$;

CREATE TRIGGER reviews_chain
    BEFORE INSERT ON corpus_reviews
    FOR EACH ROW EXECUTE FUNCTION corpus_reviews_chain();

-- Immutability (audit.events pattern, migration 0004).
CREATE OR REPLACE FUNCTION corpus_reviews_immutable() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'corpus_reviews is append-only (operation=%, seq=%)',
        TG_OP, COALESCE(OLD.seq::text, NEW.seq::text)
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER reviews_no_update
    BEFORE UPDATE ON corpus_reviews
    FOR EACH ROW EXECUTE FUNCTION corpus_reviews_immutable();

CREATE TRIGGER reviews_no_delete
    BEFORE DELETE ON corpus_reviews
    FOR EACH ROW EXECUTE FUNCTION corpus_reviews_immutable();

GRANT SELECT, INSERT ON corpus_reviews TO app_role;
GRANT SELECT, INSERT ON corpus_reviews TO tenant_writer;

ALTER TABLE corpus_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE corpus_reviews FORCE ROW LEVEL SECURITY;

-- Reviews cover global candidates; the stream is operator/DPO-facing, not
-- tenant clinical data — readable and appendable by both service roles,
-- immutable via the triggers above.
CREATE POLICY reviews_select ON corpus_reviews
    FOR SELECT TO app_role, tenant_writer USING (true);

CREATE POLICY reviews_insert ON corpus_reviews
    FOR INSERT TO app_role, tenant_writer WITH CHECK (true);

CREATE POLICY reviews_no_update_policy ON corpus_reviews
    FOR UPDATE TO app_role, tenant_writer USING (false);

CREATE POLICY reviews_no_delete_policy ON corpus_reviews
    FOR DELETE TO app_role, tenant_writer USING (false);

COMMIT;
