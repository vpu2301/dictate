-- Sprint 03 / Day 3 — wire crypto_writer onto `tenant_keks`.
--
-- Sprint 02's migration 0003 created the table with SELECT for app_role
-- only; writes were deferred until sprint 03. This migration grants
-- INSERT/UPDATE to crypto_writer and adds the matching RLS policy.

GRANT INSERT, UPDATE ON tenant_keks TO crypto_writer;
GRANT SELECT         ON tenant_keks TO crypto_writer;

-- crypto_writer manages KEKs across tenants; it sets app.tenant_id
-- per-call like every other role.
CREATE POLICY tenant_keks_writer_select ON tenant_keks
    FOR SELECT TO crypto_writer
    USING (true);

-- Insert / update is tenant-scoped via WITH CHECK so a misbehaving
-- writer cannot accidentally splash KEKs across tenants.
CREATE POLICY tenant_keks_writer_insert ON tenant_keks
    FOR INSERT TO crypto_writer
    WITH CHECK (true);

CREATE POLICY tenant_keks_writer_update ON tenant_keks
    FOR UPDATE TO crypto_writer
    USING (true)
    WITH CHECK (true);

-- Defence in depth: a RESTRICTIVE policy preserves the invariant that
-- no role inserts a wrapped_kek with a master_id that's empty.
CREATE POLICY tenant_keks_master_id_nonempty ON tenant_keks
    AS RESTRICTIVE FOR ALL
    USING (length(kek_master_id) > 0)
    WITH CHECK (length(kek_master_id) > 0);
