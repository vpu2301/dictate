-- Fix S10: autocomplete_bump_phrase_counters used '-infinity'::timestamptz
-- as a GREATEST comparison sentinel (0040) but stored the result directly.
-- A roll-up bumping a phrase with impressions and zero accepts
-- (p_last_accepted NULL) on a row whose last_accepted_at was NULL wrote
-- '-infinity' into the column. asyncpg decodes that as a timezone-naive
-- datetime, and the suggest ranking path (recency_boost) then raises
-- TypeError against its aware now() — 500ing /autocomplete/suggest for any
-- prefix whose candidate set includes a poisoned row.
--
-- 1) Collapse the sentinel back to NULL on store (NULLIF). Everything else
--    from 0040 (GREATEST never-moves-backwards semantics, delta guards,
--    SECURITY DEFINER, search_path, RLS-equivalent WHERE) is unchanged.
-- 2) Repair rows already poisoned by 0040 roll-ups. Runs as the migration
--    owner on purpose: the RESTRICTIVE update policy from 0038 blocks
--    app_role from touching system-phrase rows.
--
-- Cached tries built before this repair still carry the naive datetime
-- until their per-tenant version_tag is bumped or the TTL expires; the
-- ranking-side guard (recency_boost degrades naive datetimes to "no
-- boost") keeps them harmless in the interim.

CREATE OR REPLACE FUNCTION autocomplete_bump_phrase_counters(
    p_phrase_id     uuid,
    p_impressions   bigint,
    p_accepts       bigint,
    p_last_accepted timestamptz
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
    IF p_impressions < 0 OR p_accepts < 0 OR p_accepts > p_impressions THEN
        RAISE EXCEPTION 'invalid counter deltas: impressions=%, accepts=%',
            p_impressions, p_accepts;
    END IF;
    UPDATE autocomplete_phrases
       SET impression_count = impression_count + p_impressions,
           acceptance_count = acceptance_count + p_accepts,
           last_accepted_at = NULLIF(
               GREATEST(
                   COALESCE(last_accepted_at, '-infinity'::timestamptz),
                   COALESCE(p_last_accepted,  '-infinity'::timestamptz)
               ),
               '-infinity'::timestamptz
           ),
           updated_at       = now()
     WHERE id = p_phrase_id
       AND (
           source = 'system'::autocomplete_source
           OR tenant_id = (current_setting('app.tenant_id', true))::uuid
       );
    RETURN FOUND;
END;
$$;

UPDATE autocomplete_phrases
   SET last_accepted_at = NULL
 WHERE last_accepted_at = '-infinity'::timestamptz;
