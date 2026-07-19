DROP INDEX IF EXISTS reports_source_asr_job_unique;

ALTER TABLE reports
    DROP COLUMN IF EXISTS source_asr_job_id;
