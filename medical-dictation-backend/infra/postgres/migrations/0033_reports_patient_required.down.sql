-- Reverse of 0033_reports_patient_required.sql. Drop the CHECK first, then
-- the FK. The step-1 quarantine of legacy patient-less rows is intentionally
-- NOT reversed (a cancelled report stays cancelled).

ALTER TABLE reports DROP CONSTRAINT IF EXISTS reports_patient_required;
ALTER TABLE reports DROP CONSTRAINT IF EXISTS reports_patient_fk;
