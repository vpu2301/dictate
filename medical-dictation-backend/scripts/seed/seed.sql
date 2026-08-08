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
    -- S14 — the ONLY seeded account with tenant_admin and NOTHING else.
    -- Dev Admin A above deliberately also holds `clinician` in Keycloak (a
    -- practising doctor who runs the clinic), so it does not exercise the
    -- admin ⟂ PHI split. Log in as this one to see it: no notes, no
    -- dictations, no reports — only the patient roster and break-glass.
    ('0b000000-0000-0000-0000-00000000000b', '00000000-0000-0000-0000-00000000000a', 'owner@tenant-a.example',     'Dev Owner A',     'tenant_admin', 'active'),
    ('0c000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-00000000000a', 'clinician@tenant-a.example', 'Dev Clinician A', 'clinician',    'active'),
    ('0d000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-00000000000a', 'nurse@tenant-a.example',     'Dev Nurse A',     'nurse',        'active'),
    ('0e000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-00000000000a', 'auditor@tenant-a.example',   'Dev Auditor A',   'auditor',      'active'),
    ('0c000000-0000-0000-0000-00000000000b', '00000000-0000-0000-0000-00000000000b', 'clinician@tenant-b.example', 'Dev Clinician B', 'clinician',    'active'),
    ('0a000000-0000-0000-0000-00000000000b', '00000000-0000-0000-0000-00000000000b', 'admin@tenant-b.example',     'Dev Admin B',     'tenant_admin', 'active')
-- Conflict on (tenant_id, email), NOT on sub. The subs above are only correct
-- on a stack whose Keycloak was built from realm-export.json: Keycloak honours
-- a caller-supplied user id during realm import but NOT via its admin REST API,
-- which silently mints its own (verified on KC 24). So any account created by
-- hand on a running stack carries a different sub for the same email, and
-- conflicting on sub made this INSERT try to add a second row for that email —
-- violating users_tenant_id_email_key and aborting the whole seed. Keying on
-- the email keeps whichever sub the live Keycloak issued (the authoritative
-- one, since it is what the token carries) and refreshes the mutable columns.
ON CONFLICT (tenant_id, email) DO UPDATE
    SET display_name = EXCLUDED.display_name,
        role         = EXCLUDED.role,
        status       = EXCLUDED.status;

-- ── EVA-S01: knowledge_admin dev users (one per tenant) ─────────────────────
-- Corpus curation role for the evidence module; subs match realm-export.json.
INSERT INTO users (sub, tenant_id, email, display_name, role, status) VALUES
    ('1a000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-00000000000a', 'knowledge_admin@tenant-a.example', 'Dev Knowledge A', 'knowledge_admin', 'active'),
    ('1a000000-0000-0000-0000-00000000000b', '00000000-0000-0000-0000-00000000000b', 'knowledge_admin@tenant-b.example', 'Dev Knowledge B', 'knowledge_admin', 'active')
ON CONFLICT (tenant_id, email) DO UPDATE
    SET display_name = EXCLUDED.display_name,
        role         = EXCLUDED.role,
        status       = EXCLUDED.status;

-- ── Tenant branding (migration 0032; idempotent, dev-cosmetic) ──────────────
UPDATE tenants SET
    legal_name      = 'Dev Hospital A LLC',
    slug            = 'tenant-a',
    contact_email   = 'contact@tenant-a.example',
    phone_number    = '+380 44 000 0001',
    website         = 'https://tenant-a.example',
    address_line1   = '1 Khreshchatyk St',
    city            = 'Kyiv',
    country         = 'Ukraine',
    is_active       = true
WHERE id = '00000000-0000-0000-0000-00000000000a';

UPDATE tenants SET
    legal_name      = 'Dev Hospital B LLC',
    slug            = 'tenant-b',
    contact_email   = 'contact@tenant-b.example',
    phone_number    = '+380 44 000 0002',
    website         = 'https://tenant-b.example',
    address_line1   = '2 Deribasivska St',
    city            = 'Odesa',
    country         = 'Ukraine',
    is_active       = true
WHERE id = '00000000-0000-0000-0000-00000000000b';

-- ── Tenant memberships (migration 0032) ─────────────────────────────────────
-- Fresh dev DBs seed users here (after migrations), so the membership backfill
-- in migration 0032 finds no users; create the memberships explicitly. Map the
-- platform role to the management role. Idempotent on (tenant_id, user_sub).
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
    'active'
FROM users u
ON CONFLICT (tenant_id, user_sub) DO NOTHING;

-- Cross-tenant demo: let Dev Admin A also administer tenant-B as an admin, so
-- the tenant switcher / members UI has multi-tenant data to exercise.
INSERT INTO tenant_memberships (tenant_id, user_sub, role, status)
VALUES ('00000000-0000-0000-0000-00000000000b', '0a000000-0000-0000-0000-00000000000a', 'admin', 'active')
ON CONFLICT (tenant_id, user_sub) DO NOTHING;

-- ── Example clinic "klinic" owned by the current login account ───────────────
-- A fully-branded example tenant, with Dev Admin A (admin@tenant-a.example —
-- the account used to log in now) linked as its owner. It shows up in that
-- account's tenant switcher (GET /tenants) and tenant-settings page.
INSERT INTO tenants (
    id, name, display_name, legal_name, slug, locale, timezone, status, is_active,
    contact_email, phone_number, website,
    address_line1, city, country
) VALUES (
    '0000c111-0000-0000-0000-000000000001',
    'klinic', 'Klinic', 'Klinic Medical Center LLC', 'klinic', 'uk', 'Europe/Kyiv', 'active', true,
    'hello@klinic.example', '+380 44 111 1111', 'https://klinic.example',
    '5 Sichovykh Striltsiv St', 'Kyiv', 'Ukraine'
)
ON CONFLICT (id) DO UPDATE SET
    display_name  = EXCLUDED.display_name,
    legal_name    = EXCLUDED.legal_name,
    slug          = EXCLUDED.slug,
    contact_email = EXCLUDED.contact_email,
    phone_number  = EXCLUDED.phone_number,
    website       = EXCLUDED.website,
    address_line1 = EXCLUDED.address_line1,
    city          = EXCLUDED.city,
    country       = EXCLUDED.country,
    is_active     = EXCLUDED.is_active;

-- Link the current login account (Dev Admin A) to klinic as owner.
INSERT INTO tenant_memberships (tenant_id, user_sub, role, status)
VALUES ('0000c111-0000-0000-0000-000000000001', '0a000000-0000-0000-0000-00000000000a', 'owner', 'active')
ON CONFLICT (tenant_id, user_sub) DO NOTHING;

-- ── Klarnote's own account — the vendor, not a customer ────────────────────
-- Backs the platform-owner console at #/company (src/company/ in the SPA),
-- whose access gate is an email allowlist because there is no platform role in
-- KNOWN_ROLES yet. In Keycloak it carries tenant_admin + auditor — and
-- deliberately NOT clinician: tenant_admin covers /admin/users and /tenants/*,
-- auditor covers /audit/*, and the Usage tab's reports/sessions/ASR reads
-- resolve through tenant_admin's `stats.read` (S14) in the PHI-stripped mode.
-- Holding clinician here would grant standing report.read and silently bypass
-- the break-glass control this account is supposed to demonstrate (ADR-0033).
-- Anchored in tenant-a so the token's tid points at the tenant that
-- has seeded activity.
--
-- WHY THIS RECONCILES ON EMAIL, NOT ON A PINNED sub:
-- Keycloak honours a caller-supplied user id ONLY during realm import. Its
-- admin REST API silently mints its own (verified on KC 24), so any stack whose
-- Keycloak was provisioned by hand — i.e. every stack that was already running
-- when this account was added — carries a different sub for this email. Pinning
-- one here would then collide with users_tenant_id_email_key UNIQUE
-- (tenant_id, email) and abort the whole seed transaction. Conflicting on
-- (tenant_id, email) instead keeps whatever sub the live Keycloak issued —
-- which is the authoritative one, since it is what the token carries — and just
-- refreshes the mutable columns.
INSERT INTO users (sub, tenant_id, email, display_name, role, status)
VALUES (
    '0f000000-0000-0000-0000-00000000000f',   -- used only on a fresh realm import
    '00000000-0000-0000-0000-00000000000a',
    'vpu2301@gmail.com', 'Klarnote Owner', 'tenant_admin', 'active'
)
ON CONFLICT (tenant_id, email) DO UPDATE
    SET display_name = EXCLUDED.display_name,
        role         = EXCLUDED.role,
        status       = EXCLUDED.status;

-- ── Klarnote owner: member of every tenant ─────────────────────────────────
-- The platform-owner console reads its portfolio from GET /tenants, which
-- returns exactly the tenants the caller is a MEMBER of — auth-service serves
-- /tenants, /tenants/{id} and /tenants/{id}/members off its writer pool behind
-- a membership check, which is what makes them resolve cross-tenant. So the
-- owner's console is only as wide as its memberships: without these rows the
-- portfolio shows one tenant, not the estate.
--
-- Keyed off the email for the same reason as above — the sub is whatever the
-- live Keycloak issued. The cross join is deliberate: every tenant seeded here
-- and any added later gets a membership, so the console never silently misses a
-- clinic. Idempotent on (tenant_id, user_sub).
INSERT INTO tenant_memberships (tenant_id, user_sub, role, status)
SELECT t.id, u.sub, 'owner', 'active'
FROM tenants t
CROSS JOIN users u
WHERE u.email = 'vpu2301@gmail.com'
ON CONFLICT (tenant_id, user_sub) DO NOTHING;

COMMIT;
