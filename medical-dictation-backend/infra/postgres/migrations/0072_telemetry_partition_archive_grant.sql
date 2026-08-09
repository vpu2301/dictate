-- Sprint 16 — cold-archive read access on telemetry partitions.
--
-- Found live during S16 verification: partitions minted by the 0036
-- SECURITY DEFINER function carry NO app_role grants (ACL
-- `postgres=arwdDxt/postgres` only). Normal reads/writes go through the
-- PARENT table and never noticed — but the sprint-16 cold-archive job
-- exports a partition with `SELECT * FROM <partition>` (partition-direct
-- on purpose: the export is cross-tenant by definition, and the parent's
-- per-tenant RLS policies would blank it; partition-direct access is the
-- same sanctioned cross-tenant surface as the SECURITY DEFINER drop in
-- 0040). Without a grant the archive fails and — by the fail-safe —
-- retention stops dropping anything.
--
-- Fix: the creation function grants SELECT to app_role on each new
-- partition, and existing partitions are backfilled.

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
    -- Sprint 16: the cold-archive export reads the partition directly.
    EXECUTE format('GRANT SELECT ON %I TO app_role', v_name);
    RETURN v_name;
END;
$$;

-- Backfill existing partitions (idempotent).
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
        EXECUTE format('GRANT SELECT ON %I TO app_role', r.relname);
    END LOOP;
END;
$$;
