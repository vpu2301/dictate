-- Dev seed data — one tenant, two users, sample dictation
-- Run via: psql -U postgres -d medical_dictation -f scripts/seed/seed.sql

BEGIN;

-- ── Schema (idempotent stub — real migrations via Alembic in S2.x) ──────────

CREATE TABLE IF NOT EXISTS tenants (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    slug        TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS users (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id),
    keycloak_id TEXT NOT NULL UNIQUE,
    role        TEXT NOT NULL CHECK (role IN ('clinician', 'admin', 'reviewer')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dictations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id),
    author_id   UUID NOT NULL REFERENCES users(id),
    status      TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'processing', 'review', 'approved')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Seed data ─────────────────────────────────────────────────────────────────

INSERT INTO tenants (id, name, slug) VALUES
    ('00000000-0000-0000-0000-000000000001', 'Dev Hospital', 'dev-hospital')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO users (id, tenant_id, keycloak_id, role) VALUES
    (
        '00000000-0000-0000-0001-000000000001',
        '00000000-0000-0000-0000-000000000001',
        'dev-clinician',   -- matches Keycloak realm user
        'clinician'
    ),
    (
        '00000000-0000-0000-0001-000000000002',
        '00000000-0000-0000-0000-000000000001',
        'dev-admin',
        'admin'
    )
ON CONFLICT (keycloak_id) DO NOTHING;

INSERT INTO dictations (id, tenant_id, author_id, status) VALUES
    (
        '00000000-0000-0000-0002-000000000001',
        '00000000-0000-0000-0000-000000000001',
        '00000000-0000-0000-0001-000000000001',
        'draft'
    )
ON CONFLICT DO NOTHING;

COMMIT;
