-- Sprint 02 / Day 3 — `tenants`: the unit of multi-tenant isolation.
--
-- Row-level security ensures that `app_role` connections, scoped via
-- `app.tenant_id`, can SELECT their own tenant row and nothing else.
-- `tenant_writer` (auth-service) has full CRUD because tenant creation
-- has no incumbent tenant context.

CREATE TABLE tenants (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    locale       TEXT NOT NULL DEFAULT 'uk',
    timezone     TEXT NOT NULL DEFAULT 'Europe/Kyiv',
    status       TEXT NOT NULL CHECK (status IN ('active','suspended','dissolved'))
                 DEFAULT 'active',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Trigger to keep `updated_at` current. Lives at schema level so subsequent
-- migrations can reuse the same function.
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER tenants_set_updated_at
    BEFORE UPDATE ON tenants
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Table-level grants. RLS is the row filter on top.
GRANT SELECT                         ON tenants TO app_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON tenants TO tenant_writer;

-- Enable RLS and FORCE it so even the table owner is subject to policies
-- in normal application traffic (only the superuser can bypass).
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE  ROW LEVEL SECURITY;

-- `app_role` can read its own tenant only.
CREATE POLICY tenants_self_select ON tenants
    FOR SELECT TO app_role
    USING (id = current_setting('app.tenant_id', true)::uuid);

-- `tenant_writer` has full CRUD; used exclusively by auth-service for
-- tenant onboarding and lifecycle changes.
CREATE POLICY tenants_writer_all ON tenants
    FOR ALL TO tenant_writer
    USING (true)
    WITH CHECK (true);
