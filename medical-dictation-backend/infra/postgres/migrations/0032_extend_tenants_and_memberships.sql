-- Sprint 12 / Tenant (Clinic) branding + multi-tenant membership.
--
-- Two additive changes, no destructive edits:
--
--   1. Extend the existing `tenants` table (the RLS anchor referenced by
--      every tenant-owned FK) with the clinic/organisation profile fields
--      the SPA "Tenant" sidebar needs — legal name, slug, logo, contact
--      details and postal address — plus a simple `is_active` toggle that
--      mirrors the finer-grained `status` lifecycle column. These feed the
--      branded-PDF header (issuer_name, address, contact) and the tenant
--      settings page.
--
--   2. Introduce `tenant_memberships`: the many-to-many link between a
--      Keycloak principal (`user_sub`) and a tenant, carrying a
--      management-facing `role` (owner/admin/doctor/nurse/assistant/viewer).
--      `users` stays the per-tenant principal record (sub is its PK, so a
--      sub can only have ONE users row / home tenant); membership is the
--      broader authorization record for "which tenants may this sub reach".
--
-- The existing per-tenant isolation model is unchanged: RLS on
-- `app.tenant_id` still scopes all app_role traffic. Cross-tenant reads
-- (a user's own membership list) run on the unrestricted `tenant_writer`
-- role inside auth-service, exactly like tenant onboarding does today.

-- ── 1. tenants: branding / profile columns ──────────────────────────────

ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS legal_name          TEXT    NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS slug                TEXT,
    ADD COLUMN IF NOT EXISTS logo_url            TEXT    NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS logo_bytes          BYTEA,
    ADD COLUMN IF NOT EXISTS logo_content_type   TEXT    NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS contact_email       TEXT    NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS phone_number        TEXT    NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS website             TEXT    NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS address_line1       TEXT    NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS address_line2       TEXT    NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS postal_code         TEXT    NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS city                TEXT    NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS state_or_region     TEXT    NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS country             TEXT    NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS tax_id              TEXT    NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS registration_number TEXT    NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS is_active           BOOLEAN NOT NULL DEFAULT true;

-- Backfill sensible defaults on existing rows (idempotent):
--   * legal_name falls back to the human display_name,
--   * slug falls back to the already-unique machine `name`,
--   * is_active mirrors the current lifecycle status.
UPDATE tenants SET legal_name = display_name WHERE legal_name = '';
UPDATE tenants SET slug = name WHERE slug IS NULL;
UPDATE tenants SET is_active = (status = 'active');

-- Slug is a stable, URL-safe handle. Unique when present (existing rows are
-- seeded from `name`, which already carries a UNIQUE constraint).
CREATE UNIQUE INDEX IF NOT EXISTS tenants_slug_unique
    ON tenants (slug) WHERE slug IS NOT NULL;

-- Bound the inline logo so a runaway upload can't bloat the row / TOAST.
ALTER TABLE tenants
    DROP CONSTRAINT IF EXISTS tenants_logo_size_chk;
ALTER TABLE tenants
    ADD CONSTRAINT tenants_logo_size_chk
    CHECK (logo_bytes IS NULL OR octet_length(logo_bytes) <= 2 * 1024 * 1024);

-- ── 2. tenant_memberships ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS tenant_memberships (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- The Keycloak principal. Not an FK to `users.sub`: a membership can
    -- exist for a sub whose per-tenant `users` row lives in a different
    -- tenant (the whole point of multi-tenant access).
    user_sub    UUID NOT NULL,

    -- Management-facing role. Distinct from the platform RBAC roles carried
    -- in the JWT (tenant_admin/clinician/nurse/auditor); this drives the
    -- Tenant members UI and maps to the platform roles at the service layer.
    role        TEXT NOT NULL DEFAULT 'viewer'
                    CHECK (role IN ('owner', 'admin', 'doctor',
                                    'nurse', 'assistant', 'viewer')),

    status      TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'invited', 'suspended')),

    invited_by  UUID,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- One membership row per (tenant, principal).
    UNIQUE (tenant_id, user_sub)
);

CREATE INDEX IF NOT EXISTS tenant_memberships_tenant_idx
    ON tenant_memberships (tenant_id, role);
-- Drives the "tenants available to me" lookup (all tenants for one sub).
CREATE INDEX IF NOT EXISTS tenant_memberships_user_idx
    ON tenant_memberships (user_sub);

CREATE TRIGGER tenant_memberships_set_updated_at
    BEFORE UPDATE ON tenant_memberships
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Table-level grants: app_role reads members of its active tenant; the
-- tenant_writer role (auth-service) owns all writes and the cross-tenant
-- "my tenants" read.
GRANT SELECT                         ON tenant_memberships TO app_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_memberships TO tenant_writer;

ALTER TABLE tenant_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_memberships FORCE  ROW LEVEL SECURITY;

-- app_role: read the roster of the currently-scoped tenant only.
CREATE POLICY tenant_memberships_app_select ON tenant_memberships
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- tenant_writer: full CRUD across tenants (member management + the
-- cross-tenant membership list). Mirrors `tenants_writer_all`.
CREATE POLICY tenant_memberships_writer_all ON tenant_memberships
    FOR ALL TO tenant_writer
    USING (true)
    WITH CHECK (true);

-- RESTRICTIVE defence-in-depth: even if a future PERMISSIVE policy is added
-- too loosely, app_role traffic can only ever touch its own tenant's rows.
-- Scoped TO app_role only — tenant_writer must stay unrestricted.
CREATE POLICY tenant_memberships_app_restrictive ON tenant_memberships
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- ── 3. Backfill memberships from existing users (upgrade path) ───────────
--
-- On an already-populated database, give every existing user an active
-- membership in their home tenant, mapping the platform role to the
-- management role. On a fresh dev database this is a no-op (users are
-- seeded later by `make seed`, which creates the memberships itself).
INSERT INTO tenant_memberships (tenant_id, user_sub, role, status)
SELECT
    u.tenant_id,
    u.sub,
    CASE u.role
        WHEN 'tenant_admin' THEN 'owner'
        WHEN 'clinician'    THEN 'doctor'
        WHEN 'nurse'        THEN 'nurse'
        WHEN 'auditor'      THEN 'viewer'
        ELSE 'viewer'
    END,
    CASE WHEN u.status = 'active' THEN 'active' ELSE 'invited' END
FROM users u
ON CONFLICT (tenant_id, user_sub) DO NOTHING;
