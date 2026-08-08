-- 0067_evidence_domain.down.sql — drop in reverse dependency order.
-- NOTE: restoring the narrow role CHECK fails if knowledge_admin users exist;
-- delete them first (dev seed users only).

ALTER TABLE users DROP CONSTRAINT users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN
    ('tenant_admin', 'clinician', 'nurse', 'auditor', 'service'));

DROP TABLE IF EXISTS checks_results;
DROP TABLE IF EXISTS followups;
DROP TABLE IF EXISTS answer_traces;
DROP TABLE IF EXISTS answer_provenance;
DROP FUNCTION IF EXISTS evidence_provenance_immutable();
DROP TABLE IF EXISTS answer_segments;
DROP TABLE IF EXISTS answers;
DROP TABLE IF EXISTS questions;
DROP TABLE IF EXISTS corpus_snapshots;
DROP TABLE IF EXISTS chunks;
DROP TABLE IF EXISTS document_versions;
DROP TABLE IF EXISTS documents;
