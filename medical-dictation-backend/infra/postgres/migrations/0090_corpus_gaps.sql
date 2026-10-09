-- 0090: corpus_telemetry_gaps — the corpus-forge `gaps` work queue over
-- HTTP (GET /corpus/gaps on autocomplete-service).
--
-- The fill screen (/company/corpus/fill) points authors at the quota cells
-- that are THIN; the gaps queue is the other half of "what should I write":
-- the prefixes real users typed for which autocomplete produced zero
-- accepted suggestions, ranked by volume. Until now that list existed only
-- as `corpus-forge gaps` stdout on an operator's machine.
--
-- autocomplete_telemetry has no RLS (documented exception, ADR-0025) — the
-- per-tenant scoping there is an app-code convention, so this deliberate
-- platform-wide aggregate needs no SECURITY DEFINER. A function still
-- earns its place: it pins the query to the sprint-21 shape (the body
-- mirrors GAPS_QUERY in corpus_forge/adapters/mining.py verbatim, so the
-- CLI and the HTTP route read the same rows) and caps the limit. What
-- crosses the tenant boundary is deliberately narrow: scrubbed prefixes
-- only (prefix_scrubbed is written by the sprint-10 PII scrubber; rows
-- still carrying a redaction marker are excluded), grouped counts, no
-- tenant ids, no user ids, no timestamps. Readers hold `corpus.review` —
-- the same trust circle that already reads candidate phrases mined from
-- reports.

BEGIN;

CREATE OR REPLACE FUNCTION corpus_telemetry_gaps(p_limit integer DEFAULT 200)
RETURNS TABLE (
    prefix      text,
    impressions bigint,
    accepts     bigint,
    all_timeouts boolean
)
LANGUAGE plpgsql
STABLE
SET search_path = public, pg_temp
AS $$
BEGIN
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 5000 THEN
        RAISE EXCEPTION 'p_limit must be between 1 and 5000';
    END IF;

    RETURN QUERY
    SELECT prefix_scrubbed,
           count(*) AS impressions,
           count(*) FILTER (WHERE event_type = 'accepted') AS accepts,
           bool_and(event_type = 'timeout')                AS all_timeouts
    FROM autocomplete_telemetry
    WHERE source = 'autocomplete'
      AND prefix_scrubbed <> ''
      AND position('<redacted_PII>' IN prefix_scrubbed) = 0
    GROUP BY prefix_scrubbed
    HAVING count(*) FILTER (WHERE event_type = 'accepted') = 0
    ORDER BY count(*) DESC
    LIMIT p_limit;
END;
$$;

REVOKE ALL ON FUNCTION corpus_telemetry_gaps(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION corpus_telemetry_gaps(integer) TO app_role;

COMMIT;
