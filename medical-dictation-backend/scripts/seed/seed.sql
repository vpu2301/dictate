-- Dev seed data — aligns the DB with the Keycloak realm-export.
-- Run via: psql "$DATABASE_URL" -f scripts/seed/seed.sql
--          (or: make seed)
--
-- Authoritative schema lives in infra/postgres/migrations/0001..0002.
--   tenants(id, name, display_name, locale, timezone, status, …)
--   users(sub PK, tenant_id, email, display_name, role, status, …)
-- Roles are the five real roles: tenant_admin, clinician, nurse, auditor, service.
--
-- Tenants `tenant-a` (…00a) and `tenant-b` (…00b) are created by migration
-- 0005_seed_dev_tenants; we only (idempotently) ensure they exist here so this
-- script is self-contained, then seed the users.
--
-- IMPORTANT: each user's `sub` MUST equal the Keycloak user id pinned in
-- infra/keycloak/realm-export.json — that 1:1 mapping is what lets token `sub`
-- claims join to DB rows (and lets auth-service resolve a tenant from a sub).
-- Keep the two files in lockstep.

BEGIN;

-- ── Tenants (idempotent; owned by migration 0005) ──────────────────────────
INSERT INTO tenants (id, name, display_name, locale, timezone, status) VALUES
    ('00000000-0000-0000-0000-00000000000a', 'tenant-a', 'Dev Hospital A', 'uk', 'Europe/Kyiv', 'active'),
    ('00000000-0000-0000-0000-00000000000b', 'tenant-b', 'Dev Hospital B', 'uk', 'Europe/Kyiv', 'active')
ON CONFLICT (id) DO NOTHING;

-- ── Users (sub = Keycloak user id from realm-export.json) ───────────────────
INSERT INTO users (sub, tenant_id, email, display_name, role, status) VALUES
    ('0a000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-00000000000a', 'admin@tenant-a.example',     'Dev Admin A',     'tenant_admin', 'active'),
    ('0c000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-00000000000a', 'clinician@tenant-a.example', 'Dev Clinician A', 'clinician',    'active'),
    ('0d000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-00000000000a', 'nurse@tenant-a.example',     'Dev Nurse A',     'nurse',        'active'),
    ('0e000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-00000000000a', 'auditor@tenant-a.example',   'Dev Auditor A',   'auditor',      'active'),
    ('0c000000-0000-0000-0000-00000000000b', '00000000-0000-0000-0000-00000000000b', 'clinician@tenant-b.example', 'Dev Clinician B', 'clinician',    'active'),
    ('0a000000-0000-0000-0000-00000000000b', '00000000-0000-0000-0000-00000000000b', 'admin@tenant-b.example',     'Dev Admin B',     'tenant_admin', 'active')
ON CONFLICT (sub) DO UPDATE
    SET tenant_id    = EXCLUDED.tenant_id,
        email        = EXCLUDED.email,
        display_name = EXCLUDED.display_name,
        role         = EXCLUDED.role,
        status       = EXCLUDED.status;

COMMIT;
