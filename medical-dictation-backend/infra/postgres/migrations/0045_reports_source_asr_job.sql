-- 0045: link a report to the batch transcription job it was created from.
--
-- "Assign transcription to patient" (jobs list → patient dictation) creates
-- a report from a COMPLETE asr job's transcript. The link:
--   * lets the jobs UI show which jobs are already assigned;
--   * enforces at most ONE report per source job per tenant (the partial
--     unique index) so double-clicking "assign" can't fork two documents.
--
-- No FK to transcription_jobs: reports must survive audio/job erasure
-- (S11 right-to-erasure crypto-shreds the job's artifacts; the clinical
-- document created from it is retained under its own lifecycle).

ALTER TABLE reports
    ADD COLUMN source_asr_job_id UUID;

CREATE UNIQUE INDEX reports_source_asr_job_unique
    ON reports (tenant_id, source_asr_job_id)
    WHERE source_asr_job_id IS NOT NULL;
