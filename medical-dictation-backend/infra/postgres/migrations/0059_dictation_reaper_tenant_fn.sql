-- 0059: tenant enumeration for the dictation stale-session reaper.
--
-- Same shape, and same reason, as 0051 (and 0036/0037 before it): the
-- reaper must answer "which tenants have sessions that might be stranded?"
-- BEFORE it can open a tenant-scoped connection, and that question is
-- inherently cross-tenant.
--
-- Asked on a plain app_role connection it fails the quiet way: RLS
-- evaluates `current_setting('app.tenant_id', true)::uuid` with the setting
-- unset and either raises InvalidTextRepresentationError or filters every
-- row out — a reaper that "runs" and collects nothing, which no error log
-- would reveal.
--
-- SECURITY DEFINER runs as the owner and is the ONLY sanctioned way to see
-- across tenants. Nothing but tenant IDs leaves the function; the reaper
-- still opens `tenant_connection` per tenant to read and write the rows.

CREATE OR REPLACE FUNCTION dictation_tenants_with_stale_sessions(
    grace_seconds DOUBLE PRECISION
)
RETURNS TABLE (tenant_id UUID)
LANGUAGE sql
SECURITY DEFINER
-- Pinned search_path: a SECURITY DEFINER function without one is a
-- privilege-escalation vector (a caller-controlled search_path could
-- shadow `dictation_sessions` with their own table).
SET search_path = public, pg_temp
AS $$
    SELECT DISTINCT d.tenant_id
      FROM dictation_sessions d
     WHERE d.status IN ('creating', 'active', 'paused', 'reconnecting')
       AND d.last_active_at < now() - make_interval(secs => grace_seconds);
$$;

REVOKE ALL ON FUNCTION dictation_tenants_with_stale_sessions(DOUBLE PRECISION) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION dictation_tenants_with_stale_sessions(DOUBLE PRECISION) TO app_role;
