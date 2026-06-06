-- Sprint 02 / Day 3 — `users`: per-tenant principals.
--
-- The primary key is the Keycloak `sub` UUID — so the DB row and the IdP
-- identity are 1:1, and there is no possibility of drift on log/audit joins.
-- Tenant isolation is enforced by two policies:
--   * A PERMISSIVE policy granting access to rows whose tenant_id matches
--     the current `app.tenant_id` setting.
--   * A RESTRICTIVE policy enforcing the same predicate regardless of any
--     other policy that might be added later — defence in depth.

CREATE TABLE users (
    sub             UUID PRIMARY KEY,
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    email           TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN
                        ('tenant_admin','clinician','nurse','auditor','service')),
    status          TEXT NOT NULL CHECK (status IN
                        ('invited','active','suspended','deactivated'))
                    DEFAULT 'invited',
    mfa_enrolled_at TIMESTAMPTZ,
    last_login_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, email)
);

CREATE INDEX users_tenant_idx ON users(tenant_id);

CREATE TRIGGER users_set_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

GRANT SELECT, INSERT, UPDATE, DELETE ON users TO app_role, tenant_writer;

ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE  ROW LEVEL SECURITY;

-- PERMISSIVE: app_role can act on users in its own tenant.
CREATE POLICY users_app_role_tenant ON users
    FOR ALL TO app_role
    USING      (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- PERMISSIVE: tenant_writer scopes to the tenant set on the connection
-- (auth-service must SET app.tenant_id before user CRUD).
CREATE POLICY users_writer_tenant ON users
    FOR ALL TO tenant_writer
    USING      (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- RESTRICTIVE: defence in depth. Even if a future PERMISSIVE policy is
-- added too loosely, this one still requires tenant_id match for reads
-- and writes.
CREATE POLICY users_tenant_restrictive ON users
    AS RESTRICTIVE FOR ALL
    USING      (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
