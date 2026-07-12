-- Sprint 10 step-05 fixes.
--
-- 1) autocomplete_bump_phrase_counters: last_accepted_at must use GREATEST,
--    not COALESCE — a manual roll-up re-run for an OLD day (--day) would
--    otherwise move last_accepted_at BACKWARDS and wrongly re-inflate the
--    recency boost window.
--
-- 2) autocomplete_drop_telemetry_partition: the sanctioned DDL path for the
--    90-day retention leg of partition rotation (app_role owns no DDL —
--    same doctrine as 0036/0037). Deliberately narrow: only drops monthly
--    partitions of autocomplete_telemetry whose RANGE ended more than
--    90 days ago; name derived server-side. Cold-storage archival before
--    drop is named sprint-16 scope.

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
           last_accepted_at = GREATEST(
               COALESCE(last_accepted_at, '-infinity'::timestamptz),
               COALESCE(p_last_accepted,  '-infinity'::timestamptz)
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

CREATE OR REPLACE FUNCTION autocomplete_drop_telemetry_partition(
    p_start date
) RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_name text;
    v_end  date;
BEGIN
    IF date_trunc('month', p_start)::date <> p_start THEN
        RAISE EXCEPTION 'partition start must be a month boundary, got %', p_start;
    END IF;
    v_end := (p_start + interval '1 month')::date;
    IF v_end > (now()::date - 90) THEN
        RAISE EXCEPTION
            'refusing to drop partition ending % — inside the 90-day retention window',
            v_end;
    END IF;
    v_name := 'autocomplete_telemetry_' || to_char(p_start, 'YYYY_MM');
    IF to_regclass(v_name) IS NULL THEN
        RETURN NULL; -- already gone: idempotent
    END IF;
    EXECUTE format(
        'ALTER TABLE autocomplete_telemetry DETACH PARTITION %I', v_name
    );
    EXECUTE format('DROP TABLE %I', v_name);
    RETURN v_name;
END;
$$;

REVOKE ALL ON FUNCTION autocomplete_drop_telemetry_partition(date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION autocomplete_drop_telemetry_partition(date) TO app_role;
