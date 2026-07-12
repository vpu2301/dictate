-- Reverse 0040: restore the COALESCE counter function (0037 behaviour)
-- and drop the retention helper. Dropped partitions are not restored.

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

DROP FUNCTION IF EXISTS autocomplete_drop_telemetry_partition(date);
