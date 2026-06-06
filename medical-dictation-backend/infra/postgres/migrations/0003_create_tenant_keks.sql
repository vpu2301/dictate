-- Sprint 02 / Day 3 — `tenant_keks`: placeholder for sprint 3.
--
-- Per ADR-0007 (RLS-first) every table must enable RLS at creation time
-- so that a forgotten policy fails *closed*, not open. Sprint 3 will
-- introduce a `crypto_writer` role and the corresponding write policy
-- analogous to `tenant_writer` for tenants.

CREATE TABLE tenant_keks (
    tenant_id     UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE RESTRICT,
    wrapped_kek   BYTEA NOT NULL,
    kek_master_id TEXT  NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    rotated_at    TIMESTAMPTZ
);

GRANT SELECT ON tenant_keks TO app_role;
-- Writes will be granted to crypto_writer in sprint 3.

ALTER TABLE tenant_keks ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_keks FORCE  ROW LEVEL SECURITY;

CREATE POLICY tenant_keks_self_select ON tenant_keks
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- No INSERT/UPDATE/DELETE policy yet — sprint 3 wires `crypto_writer`.
