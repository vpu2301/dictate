-- Reverse of 0031_create_patients.sql. Drop in FK-dependency order
-- (children first); CASCADE mops up policies + indexes.

DROP TABLE IF EXISTS patient_privacy_requests;
DROP TABLE IF EXISTS patient_anamnesis;
DROP TABLE IF EXISTS patient_consents;
DROP TABLE IF EXISTS clinical_notes;
DROP TABLE IF EXISTS encounters;
DROP TABLE IF EXISTS patients;
