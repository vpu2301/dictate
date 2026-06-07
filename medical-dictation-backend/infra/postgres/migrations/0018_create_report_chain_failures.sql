-- Sprint 08 / Day 8 — chain reconciler scratch table.
-- A separate, audit-schema-resident table that the reconciler appends
-- to whenever it detects a chain anomaly. Used as the source-of-truth
-- for the chain integrity dashboard + alerting; the audit_events
-- log carries the same data with hash-chained guarantees.

CREATE TABLE audit.report_chain_failures (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    detected_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id     UUID NOT NULL,
    report_id     UUID NOT NULL,
    anomaly_kind  TEXT NOT NULL CHECK (anomaly_kind IN (
        'gap_in_version_numbers',
        'cycle_detected',
        'unreachable_from_head',
        'amendment_off_unsigned_parent',
        'multiple_genesis_versions',
        'parent_missing'
    )),
    detail_jsonb  JSONB NOT NULL DEFAULT '{}'::jsonb,
    resolved_at   TIMESTAMPTZ,
    resolved_by   UUID,
    resolution_notes TEXT
);

CREATE INDEX report_chain_failures_unresolved_idx
    ON audit.report_chain_failures (detected_at DESC)
    WHERE resolved_at IS NULL;
CREATE INDEX report_chain_failures_by_report_idx
    ON audit.report_chain_failures (report_id, detected_at DESC);

GRANT SELECT, INSERT ON audit.report_chain_failures TO audit_writer;
GRANT SELECT, UPDATE ON audit.report_chain_failures TO audit_writer;
GRANT SELECT ON audit.report_chain_failures TO audit_reader;

-- ── RLS (Sprint A1) ─────────────────────────────────────────────────
-- This table carries tenant_id but shipped with no RLS — a cross-tenant
-- isolation gap. It mirrors audit.events (same privileged roles, same
-- per-tenant access). The reconciler already SETs app.tenant_id per tenant
-- before writing audit.events, so tenant-scoped policies are compatible.
ALTER TABLE audit.report_chain_failures ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit.report_chain_failures FORCE  ROW LEVEL SECURITY;

CREATE POLICY report_chain_failures_read ON audit.report_chain_failures
    FOR SELECT TO audit_writer, audit_reader
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY report_chain_failures_writer_insert ON audit.report_chain_failures
    FOR INSERT TO audit_writer
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY report_chain_failures_writer_update ON audit.report_chain_failures
    FOR UPDATE TO audit_writer
    USING      (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- Defence-in-depth: enforce tenant scope regardless of any future policy.
CREATE POLICY report_chain_failures_restrictive ON audit.report_chain_failures
    AS RESTRICTIVE FOR ALL TO audit_writer, audit_reader
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
