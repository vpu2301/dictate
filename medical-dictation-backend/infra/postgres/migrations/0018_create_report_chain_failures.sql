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
