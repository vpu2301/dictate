-- 0068_evidence_ingest.down.sql

-- Restore the strict (0067) corpus policies.

DROP POLICY documents_tenant_select ON documents;
CREATE POLICY documents_tenant_select ON documents
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
DROP POLICY documents_tenant_restrictive ON documents;
CREATE POLICY documents_tenant_restrictive ON documents
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

DROP POLICY document_versions_tenant_select ON document_versions;
CREATE POLICY document_versions_tenant_select ON document_versions
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
DROP POLICY document_versions_tenant_restrictive ON document_versions;
CREATE POLICY document_versions_tenant_restrictive ON document_versions
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

DROP POLICY IF EXISTS chunks_tenant_update ON chunks;
REVOKE UPDATE ON chunks FROM app_role;

DROP POLICY chunks_tenant_select ON chunks;
CREATE POLICY chunks_tenant_select ON chunks
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
DROP POLICY chunks_tenant_restrictive ON chunks;
CREATE POLICY chunks_tenant_restrictive ON chunks
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

DROP POLICY corpus_snapshots_tenant_select ON corpus_snapshots;
CREATE POLICY corpus_snapshots_tenant_select ON corpus_snapshots
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
DROP POLICY corpus_snapshots_tenant_restrictive ON corpus_snapshots;
CREATE POLICY corpus_snapshots_tenant_restrictive ON corpus_snapshots
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

DROP TABLE IF EXISTS quarantine;
DROP TABLE IF EXISTS ingest_errors;
DROP TABLE IF EXISTS ingest_jobs;

-- Remove the reserved global-corpus tenant only if nothing references it yet.
DELETE FROM tenants t
WHERE t.id = '00000000-0000-0000-0000-000000000000'
  AND NOT EXISTS (SELECT 1 FROM tenant_keks k WHERE k.tenant_id = t.id)
  AND NOT EXISTS (SELECT 1 FROM documents d WHERE d.tenant_id = t.id);
