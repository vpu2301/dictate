-- 0077: tenant enumeration for the stranded-ASR-job reaper.
--
-- Same shape, and same reason, as 0059 (and 0051/0036 before it): the
-- reaper must answer "which tenants hold a job that no worker is coming
-- back for?" BEFORE it can open a tenant-scoped connection, and that
-- question is inherently cross-tenant.
--
-- Asked on a plain app_role connection it fails the quiet way: RLS
-- evaluates `current_setting('app.tenant_id', true)::uuid` with the setting
-- unset and either raises InvalidTextRepresentationError or filters every
-- row out — a reaper that "runs" and collects nothing, which no error log
-- would reveal.
--
-- What is being collected, and why it can exist at all:
--
--   status='running' past the grace window
--       The worker marked the job running and then died — SIGKILL, OOM
--       kill, node eviction. Nothing else in the system ever revisits that
--       row: the queue redelivers the message (so the transcript may still
--       be produced by another worker) but the row itself is only ever
--       written by the worker that owns it.
--
--   status='queued' past the (longer) grace window
--       The row was committed and the enqueue reported success, but the
--       message did not survive to be claimed — a flushed Redis, a stream
--       trimmed under load, a consumer group recreated.
--
-- Both leave a row that holds a slot in `per_tenant_concurrent_jobs` and
-- shows the clinician a job that is never going to resolve.
--
-- SECURITY DEFINER runs as the owner and is the ONLY sanctioned way to see
-- across tenants. Nothing but tenant IDs leaves the function; the reaper
-- still opens `tenant_connection` per tenant to read and write the rows.

CREATE OR REPLACE FUNCTION asr_tenants_with_stale_jobs(
    running_grace_seconds DOUBLE PRECISION,
    queued_grace_seconds  DOUBLE PRECISION
)
RETURNS TABLE (tenant_id UUID)
LANGUAGE sql
SECURITY DEFINER
-- Pinned search_path: a SECURITY DEFINER function without one is a
-- privilege-escalation vector (a caller-controlled search_path could
-- shadow `transcription_jobs` with their own table).
SET search_path = public, pg_temp
AS $$
    SELECT DISTINCT j.tenant_id
      FROM transcription_jobs j
     WHERE (j.status = 'running'
            AND j.started_at < now() - make_interval(secs => running_grace_seconds))
        OR (j.status = 'queued'
            AND j.queued_at  < now() - make_interval(secs => queued_grace_seconds));
$$;

REVOKE ALL ON FUNCTION asr_tenants_with_stale_jobs(DOUBLE PRECISION, DOUBLE PRECISION)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION asr_tenants_with_stale_jobs(DOUBLE PRECISION, DOUBLE PRECISION)
    TO app_role;

-- The reaper's scan predicate. Without it every sweep is a seq scan over
-- every job the tenant has ever submitted, on a table that only grows.
CREATE INDEX IF NOT EXISTS transcription_jobs_inflight_idx
    ON transcription_jobs (status, started_at, queued_at)
    WHERE status IN ('queued', 'running');
