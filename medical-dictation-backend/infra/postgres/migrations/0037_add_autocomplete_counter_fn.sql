-- Sprint 10 fix — sanctioned counter-bump path for the nightly roll-up.
--
-- The roll-up runs as app_role under tenant_connection. The RESTRICTIVE
-- update_user_phrases policy (migration 0023) limits UPDATE to user-owned
-- rows (or tenant rows for admins) — correct for the phrase-write API, but
-- it silently no-opped the roll-up's counter UPDATE on *system* phrases:
-- the aggregate ran, autocomplete_rollup_progress was marked done, and the
-- ranking counters never moved. System phrases are the bulk of accepts, so
-- the accept → roll-up → counters → better-ranking loop never closed.
--
-- This SECURITY DEFINER function can ONLY increment the three counter
-- columns, and only on rows the calling tenant may see (system rows or the
-- tenant's own): text/scope/ownership stay untouchable by app_role.

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
           last_accepted_at = COALESCE(p_last_accepted, last_accepted_at),
           updated_at       = now()
     WHERE id = p_phrase_id
       AND (
           source = 'system'::autocomplete_source
           OR tenant_id = (current_setting('app.tenant_id', true))::uuid
       );
    RETURN FOUND;
END;
$$;

REVOKE ALL ON FUNCTION autocomplete_bump_phrase_counters(uuid, bigint, bigint, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION autocomplete_bump_phrase_counters(uuid, bigint, bigint, timestamptz) TO app_role;
