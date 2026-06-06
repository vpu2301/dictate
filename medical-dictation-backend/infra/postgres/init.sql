-- Idempotent first-start initialisation for the dev Postgres container.
-- Re-runs cleanly because every CREATE wraps in a guard.
-- Sprint 02 introduces RLS policies on real tables; sprint 01 provisions the
-- roles, schemas, and extensions those policies will rely on.

-- ── Keycloak side database ────────────────────────────────────────────
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'keycloak') THEN
    CREATE ROLE keycloak LOGIN PASSWORD 'keycloak';
  END IF;
END
$$;

SELECT 'CREATE DATABASE keycloak OWNER keycloak'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'keycloak')
\gexec

GRANT ALL PRIVILEGES ON DATABASE keycloak TO keycloak;

-- ── Application roles ─────────────────────────────────────────────────
-- None of these roles have BYPASSRLS. The DBA superuser (`postgres`) does,
-- but is never used by service code.
DO $$
BEGIN
  -- app_role: the role services connect as for tenant-scoped reads/writes.
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_role') THEN
    CREATE ROLE app_role LOGIN PASSWORD 'app_role';
  END IF;

  -- tenant_writer: the only role permitted to write to `tenants` and to
  -- manage `users` across tenants. Used exclusively by auth-service for
  -- tenant CRUD and user invite/deactivate flows. Still RLS-bound on
  -- `users` (sets app.tenant_id per-call); `tenants` is unconstrained
  -- because tenant creation has no incumbent tenant context.
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'tenant_writer') THEN
    CREATE ROLE tenant_writer LOGIN PASSWORD 'tenant_writer';
  END IF;

  -- audit_writer: the only role permitted to INSERT into audit.* (S2 grants).
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'audit_writer') THEN
    CREATE ROLE audit_writer LOGIN PASSWORD 'audit_writer';
  END IF;

  -- audit_reader: read-only access for SIEM / compliance exports.
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'audit_reader') THEN
    CREATE ROLE audit_reader LOGIN PASSWORD 'audit_reader';
  END IF;

  -- crypto_writer (sprint 03): the only role permitted to INSERT/UPDATE
  -- on tenant_keks. asr-service and asr-worker use this dedicated role
  -- so that a compromise of the broader app_role does not yield write
  -- access to the wrapped-KEK store. SELECT is granted to app_role
  -- (RLS-bound) so existing reads keep working; crypto_writer's writes
  -- are also RLS-bound (no row leaves a tenant's scope).
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'crypto_writer') THEN
    CREATE ROLE crypto_writer LOGIN PASSWORD 'crypto_writer';
  END IF;
END
$$;

-- ── Application database — extensions & schemas ──────────────────────
\c medical_dictation

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- public schema is created by Postgres; ensure ownership semantics are sane.
GRANT USAGE ON SCHEMA public TO app_role, tenant_writer, crypto_writer;
GRANT CREATE ON SCHEMA public TO app_role;

CREATE SCHEMA IF NOT EXISTS audit;
GRANT USAGE ON SCHEMA audit TO audit_writer, audit_reader;
GRANT CREATE ON SCHEMA audit TO audit_writer;

-- Default privileges so future tables created in `audit` are readable by the
-- reader role and writable by the writer role without per-table grants.
ALTER DEFAULT PRIVILEGES IN SCHEMA audit
  GRANT SELECT ON TABLES TO audit_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA audit
  GRANT INSERT, SELECT ON TABLES TO audit_writer;

-- ── Keycloak database extensions and ownership ───────────────────────
\c keycloak
CREATE EXTENSION IF NOT EXISTS pgcrypto;
-- pg15+ revokes CREATE on `public` from non-owners by default. Liquibase
-- (Keycloak's schema migrator) needs to create tables in `public`, so the
-- keycloak role must own the schema.
ALTER SCHEMA public OWNER TO keycloak;
GRANT ALL ON SCHEMA public TO keycloak;
