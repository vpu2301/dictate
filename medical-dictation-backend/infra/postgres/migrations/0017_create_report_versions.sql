-- Sprint 08 / Day 1 — report_versions (append-only).
--
-- Every change to a report's content emits a new row here. Rows are
-- never UPDATEd or DELETEd. The chain integrity property test (CI) +
-- daily reconciler (sprint-08 day-8) verify this invariant.
--
-- ``parent_version_id`` chains amendments back to the signed version
-- they amend. For non-amendment versions parent is the previous
-- version_number (always-on linear chain). For amendments parent is
-- the *signed* version we amended off, so the chain forms a tree.

CREATE TABLE report_versions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    report_id           UUID NOT NULL REFERENCES reports(id) ON DELETE RESTRICT,

    -- 1-based, monotonic per report. (Validated by chain reconciler.)
    version_number      INTEGER NOT NULL CHECK (version_number >= 1),

    -- Parent in the version DAG.
    parent_version_id   UUID REFERENCES report_versions(id) ON DELETE RESTRICT,

    -- Who created this version + when.
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Content body. Strict shape enforced by ReportContent Pydantic
    -- (libs/report_models). DB does not validate beyond JSON-validity.
    content_jsonb       JSONB NOT NULL,
    -- Plain-text projection of content_jsonb for FTS. Service layer
    -- maintains this; never edited by hand.
    rendered_text       TEXT NOT NULL DEFAULT '',
    -- Diff from parent_version (one entry per section change). Empty
    -- for v1. Service computes via difflib.SequenceMatcher.
    diff_jsonb          JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Free-form metadata bag (body_hash for idempotency, FE client
    -- info, source dictation segment ids, etc.). Open dict; the
    -- ``metadata.body_hash`` key is reserved for autosave idempotency.
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Amendment fields. Filled in by POST /reports/{id}/amend.
    is_amendment        BOOLEAN NOT NULL DEFAULT FALSE,
    amendment_type      report_amendment_type,
    amendment_reason    TEXT,

    -- Signing fields (sprint-09 fills these in via KEP).
    signed_at           TIMESTAMPTZ,
    signed_by           UUID REFERENCES users(id) ON DELETE RESTRICT,
    signing_record_id   UUID,           -- FK to signing_records (sprint-09)
    signed_data         BYTEA,          -- canonical bytes that were signed
    signed_data_hash    BYTEA,          -- sha256(signed_data) for quick check

    -- Generated tsvector on rendered_text. Used by full-text search.
    search_vector       tsvector GENERATED ALWAYS AS (
                          to_tsvector('simple', coalesce(rendered_text, ''))
                        ) STORED,

    CONSTRAINT report_versions_unique_per_report
        UNIQUE (report_id, version_number),
    CONSTRAINT report_versions_amendment_consistency
        CHECK ((is_amendment = TRUE) = (amendment_type IS NOT NULL))
);

CREATE INDEX report_versions_report_idx
    ON report_versions (report_id, version_number DESC);
CREATE INDEX report_versions_parent_idx
    ON report_versions (parent_version_id);
CREATE INDEX report_versions_search_vector_idx
    ON report_versions USING gin (search_vector);
CREATE INDEX report_versions_metadata_body_hash_idx
    ON report_versions ((metadata->>'body_hash'));
CREATE INDEX report_versions_signed_idx
    ON report_versions (signed_at)
    WHERE signed_at IS NOT NULL;

-- Wire reports.current_version_id now that the target table exists.
ALTER TABLE reports
    ADD CONSTRAINT reports_current_version_fk
    FOREIGN KEY (current_version_id)
    REFERENCES report_versions(id)
    DEFERRABLE INITIALLY DEFERRED;

-- ── RLS via JOIN to reports.tenant_id ───────────────────────────────
-- report_versions does not carry tenant_id (it's denormalized one row
-- away on reports). RLS therefore uses an EXISTS subquery — slower
-- than a direct column check but correct. Postgres pushes the
-- predicate into the join when the planner is happy; see day-9
-- EXPLAIN ANALYZE doc.

ALTER TABLE report_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE report_versions FORCE  ROW LEVEL SECURITY;

CREATE POLICY report_versions_tenant_select ON report_versions
    FOR SELECT TO app_role
    USING (
        EXISTS (
            SELECT 1 FROM reports r
            WHERE r.id = report_versions.report_id
              AND r.tenant_id = current_setting('app.tenant_id', true)::uuid
        )
    );

CREATE POLICY report_versions_tenant_insert ON report_versions
    FOR INSERT TO app_role
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM reports r
            WHERE r.id = report_versions.report_id
              AND r.tenant_id = current_setting('app.tenant_id', true)::uuid
        )
    );

-- UPDATE on report_versions is reserved for the sprint-09 signing
-- path which fills in signed_at / signed_by / signing_record_id /
-- signed_data. Sprint-08 service code never UPDATEs.
CREATE POLICY report_versions_tenant_update ON report_versions
    FOR UPDATE TO app_role
    USING (
        EXISTS (
            SELECT 1 FROM reports r
            WHERE r.id = report_versions.report_id
              AND r.tenant_id = current_setting('app.tenant_id', true)::uuid
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM reports r
            WHERE r.id = report_versions.report_id
              AND r.tenant_id = current_setting('app.tenant_id', true)::uuid
        )
    );

CREATE POLICY report_versions_tenant_delete ON report_versions
    FOR DELETE TO app_role
    USING (false);  -- append-only forever

CREATE POLICY report_versions_tenant_restrictive ON report_versions
    AS RESTRICTIVE
    FOR ALL TO app_role
    USING (
        EXISTS (
            SELECT 1 FROM reports r
            WHERE r.id = report_versions.report_id
              AND r.tenant_id = current_setting('app.tenant_id', true)::uuid
        )
    );

GRANT SELECT, INSERT, UPDATE ON report_versions TO app_role;
