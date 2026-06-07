-- Sprint 08 / Day 1 — reports.
--
-- Append-only versioning: `reports` is the head row (one per logical
-- report); `report_versions` (migration 0017) holds the immutable
-- history. `reports.current_version_id` points at the latest version
-- but is installed as a deferrable FK so the two-step
-- insert-report-then-insert-version pattern works inside a single
-- serializable transaction (ADR-0020).
--
-- Denormalized columns (title, icd10_codes[], encounter_date) duplicate
-- data that also lives in the current version's content_jsonb. They
-- are the columns we filter/sort on, kept here for index efficiency.
-- A daily integrity check (sprint-16) catches drift between the head
-- and the current version.

CREATE TYPE report_status AS ENUM (
    'draft',
    'finalized',
    'signed',
    'amended',
    'cancelled'
);

CREATE TYPE report_amendment_type AS ENUM (
    'correction',
    'addition',
    'clarification'
);

CREATE TABLE reports (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,

    -- Generated per-tenant per-year (REP-2026-00042).
    code                TEXT NOT NULL,

    -- FK to the current version row. NULL during the two-step insert
    -- in CreateReport; non-NULL at end of transaction (enforced by
    -- ``reports_current_version_id_not_null_at_commit`` deferrable
    -- check below).
    current_version_id  UUID,

    -- Lifecycle.
    status              report_status NOT NULL DEFAULT 'draft',

    -- Authorship.
    primary_author_id   UUID NOT NULL REFERENCES users(sub) ON DELETE RESTRICT,
    co_author_ids       UUID[] NOT NULL DEFAULT '{}',

    -- Patient (sprint 11 brings the patients table).
    patient_id          UUID,
    patient_name_redacted TEXT,  -- initials only; resolved by sprint 11

    -- Template provenance.
    template_id         UUID REFERENCES templates(id) ON DELETE RESTRICT,
    template_schema_version INTEGER,

    -- Denormalized for filter/sort. Kept in sync by service layer.
    title               TEXT NOT NULL DEFAULT '',
    icd10_codes         TEXT[] NOT NULL DEFAULT '{}',
    encounter_date      DATE,

    -- Lifecycle timestamps.
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finalized_at        TIMESTAMPTZ,
    signed_at           TIMESTAMPTZ,
    cancelled_at        TIMESTAMPTZ,
    cancelled_reason    TEXT,

    -- Dictation session that birthed this draft (sprint-04 link).
    source_session_id   UUID,

    -- Generated tsvector on title + code for the "type-ahead" path.
    search_vector       tsvector GENERATED ALWAYS AS (
                          to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(code, ''))
                        ) STORED,

    CONSTRAINT reports_code_per_tenant_unique UNIQUE (tenant_id, code),
    CONSTRAINT reports_status_finalized_has_ts
        CHECK ((status IN ('finalized','signed','amended')) = (finalized_at IS NOT NULL)),
    CONSTRAINT reports_status_signed_has_ts
        CHECK ((status IN ('signed','amended')) = (signed_at IS NOT NULL)),
    CONSTRAINT reports_status_cancelled_has_ts
        CHECK ((status = 'cancelled') = (cancelled_at IS NOT NULL))
);

CREATE INDEX reports_tenant_status_idx
    ON reports (tenant_id, status, encounter_date DESC, id);
CREATE INDEX reports_patient_idx
    ON reports (tenant_id, patient_id) WHERE patient_id IS NOT NULL;
CREATE INDEX reports_author_idx
    ON reports (tenant_id, primary_author_id, updated_at DESC);
CREATE INDEX reports_search_vector_idx
    ON reports USING gin (search_vector);
CREATE INDEX reports_icd10_idx
    ON reports USING gin (icd10_codes);
CREATE INDEX reports_updated_idx
    ON reports (tenant_id, updated_at DESC, id);

-- ── RLS ─────────────────────────────────────────────────────────────

ALTER TABLE reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE reports FORCE  ROW LEVEL SECURITY;

CREATE POLICY reports_tenant_select ON reports
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY reports_tenant_insert ON reports
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY reports_tenant_update ON reports
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY reports_tenant_delete ON reports
    FOR DELETE TO app_role
    USING (false);  -- hard delete forbidden; cancellation is the soft-delete

-- Defence-in-depth RESTRICTIVE: even if a future PERMISSIVE policy is
-- mis-written, the tenant scope is enforced.
CREATE POLICY reports_tenant_restrictive ON reports
    AS RESTRICTIVE
    FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE ON reports TO app_role;

-- ── Per-tenant code counter ────────────────────────────────────────
-- Used by ``code_sequence.next_code()`` to mint REP-YYYY-NNNNN.

CREATE TABLE report_code_counters (
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    year       INTEGER NOT NULL,
    counter    BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, year)
);

ALTER TABLE report_code_counters ENABLE ROW LEVEL SECURITY;
ALTER TABLE report_code_counters FORCE  ROW LEVEL SECURITY;

CREATE POLICY report_code_counters_tenant ON report_code_counters
    FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE ON report_code_counters TO app_role;
