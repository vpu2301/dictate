-- Down for 0077 — drop the reaper's tenant-enumeration function and index.
-- Jobs stranded by a dead worker go back to sitting in `running` forever.

DROP INDEX IF EXISTS transcription_jobs_inflight_idx;
DROP FUNCTION IF EXISTS asr_tenants_with_stale_jobs(DOUBLE PRECISION, DOUBLE PRECISION);
