-- Sprint 10 / Day 1 — autocomplete_telemetry (partitioned monthly).
--
-- High-volume event log. tenant_id is on every row; reads always
-- filter by tenant_id. No RLS (documented exception per ADR-0025);
-- the RLS-policy CI gate has this table whitelisted.

CREATE TABLE autocomplete_telemetry (
    id              BIGSERIAL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id       UUID NOT NULL,
    user_id         UUID NOT NULL,
    request_id      UUID NOT NULL,
    event_type      TEXT NOT NULL CHECK (event_type IN ('shown_only', 'accepted', 'rejected', 'timeout')),
    phrase_id       UUID,                 -- null when snippet
    snippet_id      UUID,                 -- null when phrase
    prefix_scrubbed TEXT NOT NULL,        -- PII-scrubbed before insert
    context_jsonb   JSONB NOT NULL DEFAULT '{}'::jsonb,

    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

-- Seed two partitions: current + next month.
-- Range covers 2026-05 and 2026-06 (sprint-10 start dates). Production
-- migration runner re-applies safely; sprint-16 will introduce
-- partman.
CREATE TABLE autocomplete_telemetry_2026_05 PARTITION OF autocomplete_telemetry
    FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');
CREATE TABLE autocomplete_telemetry_2026_06 PARTITION OF autocomplete_telemetry
    FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');

CREATE INDEX autocomplete_telemetry_phrase_idx
    ON autocomplete_telemetry (phrase_id, created_at DESC)
    WHERE event_type = 'accepted';
CREATE INDEX autocomplete_telemetry_tenant_idx
    ON autocomplete_telemetry (tenant_id, created_at DESC);
CREATE INDEX autocomplete_telemetry_user_idx
    ON autocomplete_telemetry (user_id, created_at DESC);
CREATE INDEX autocomplete_telemetry_snippet_idx
    ON autocomplete_telemetry (snippet_id, created_at DESC)
    WHERE event_type = 'accepted' AND snippet_id IS NOT NULL;

GRANT SELECT, INSERT ON autocomplete_telemetry TO app_role;
GRANT USAGE ON SEQUENCE autocomplete_telemetry_id_seq TO app_role;

-- Rollup completion markers — idempotency for the nightly job.
CREATE TABLE autocomplete_rollup_progress (
    rollup_date     DATE NOT NULL,
    tenant_id       UUID NOT NULL,
    finished_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    events_processed BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (rollup_date, tenant_id)
);
GRANT SELECT, INSERT ON autocomplete_rollup_progress TO app_role;
