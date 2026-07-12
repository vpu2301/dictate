-- Sprint 10 fix — sanctioned DDL path for telemetry partition rotation.
--
-- The in-service rotation job ran as app_role, which owns no DDL, so
-- CREATE TABLE ... PARTITION OF failed with InsufficientPrivilegeError.
-- Once the migration-seeded partitions (2026-05/06) ran out, every
-- telemetry insert was silently dropped (fire-and-forget path).
--
-- This SECURITY DEFINER function is deliberately narrow: it can only
-- create month-boundary partitions of autocomplete_telemetry, with the
-- name derived server-side from the validated bounds. app_role gets
-- EXECUTE on it and nothing else.

CREATE OR REPLACE FUNCTION autocomplete_create_telemetry_partition(
    p_start date,
    p_end   date
) RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_name text;
BEGIN
    IF date_trunc('month', p_start)::date <> p_start
       OR (p_start + interval '1 month')::date <> p_end THEN
        RAISE EXCEPTION
            'partition bounds must be consecutive month starts, got % .. %',
            p_start, p_end;
    END IF;
    v_name := 'autocomplete_telemetry_' || to_char(p_start, 'YYYY_MM');
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %I PARTITION OF autocomplete_telemetry '
        'FOR VALUES FROM (%L) TO (%L)',
        v_name, p_start, p_end
    );
    RETURN v_name;
END;
$$;

REVOKE ALL ON FUNCTION autocomplete_create_telemetry_partition(date, date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION autocomplete_create_telemetry_partition(date, date) TO app_role;

-- Backfill the gap left while rotation was broken (idempotent).
SELECT autocomplete_create_telemetry_partition('2026-07-01', '2026-08-01');
SELECT autocomplete_create_telemetry_partition('2026-08-01', '2026-09-01');
