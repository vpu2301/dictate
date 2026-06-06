-- Sprint 02 / Day 6 — Seed the two dev tenants referenced by the Keycloak
-- realm-export.json. Idempotent on re-apply (ON CONFLICT DO NOTHING).
--
-- Production environments do *not* apply this migration: tenants are
-- created by the auth-service onboarding flow (not yet built — sprint 17).
-- Gating this behind a checked-in migration keeps the dev experience
-- one-command (`make migrate-up`) without leaking dev data into prod.

INSERT INTO tenants (id, name, display_name, locale, timezone, status)
VALUES
    ('00000000-0000-0000-0000-00000000000a', 'tenant-a', 'Dev Tenant A', 'uk', 'Europe/Kyiv', 'active'),
    ('00000000-0000-0000-0000-00000000000b', 'tenant-b', 'Dev Tenant B', 'uk', 'Europe/Kyiv', 'active')
ON CONFLICT (id) DO NOTHING;
