DROP POLICY IF EXISTS tenant_keks_master_id_nonempty ON tenant_keks;
DROP POLICY IF EXISTS tenant_keks_writer_update ON tenant_keks;
DROP POLICY IF EXISTS tenant_keks_writer_insert ON tenant_keks;
DROP POLICY IF EXISTS tenant_keks_writer_select ON tenant_keks;
REVOKE INSERT, UPDATE, SELECT ON tenant_keks FROM crypto_writer;
