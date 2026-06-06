-- Sprint 09 / Day 2 — signing_provider_health.
-- Singleton-per-provider; updated by the 30s health monitor job.

CREATE TABLE signing_provider_health (
    provider             signing_provider PRIMARY KEY,
    healthy              BOOLEAN NOT NULL DEFAULT true,
    last_check_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT
);

-- Global table (no tenant_id) — health is system-wide.
GRANT SELECT, INSERT, UPDATE ON signing_provider_health TO app_role;
