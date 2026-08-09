-- Sprint 16 rollback — restore the 0036 function body (no per-partition
-- grant) and revoke the backfilled partition SELECTs.

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

DO $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT c.relname
        FROM pg_class c
        JOIN pg_inherits i ON c.oid = i.inhrelid
        WHERE i.inhparent = 'autocomplete_telemetry'::regclass
    LOOP
        EXECUTE format('REVOKE SELECT ON %I FROM app_role', r.relname);
    END LOOP;
END;
$$;
