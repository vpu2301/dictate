-- 0082: corpus_candidates staging table (sprint 21, ADR-0043 §3).
--
-- Everything lands here before it earns a row in autocomplete_phrases; the
-- promote job is the only path in. tenant_id NULL = a global (system-corpus)
-- candidate — mined n-grams are global by construction (k-anonymity requires
-- ≥2 tenants, so no single tenant owns them); tenant-scoped candidates are
-- possible for future per-tenant corpus work and follow app-role RLS.
--
-- Rejected candidates are never deleted: they are the calibration ground
-- truth and the dedupe backstop (ADR-0043 consequences).

BEGIN;

CREATE TABLE corpus_candidates (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid        REFERENCES tenants(id) ON DELETE CASCADE,
    language            text        NOT NULL CHECK (language IN ('uk', 'en')),
    specialty           text,
    section_hint        text,
    phrase              text        NOT NULL
        CONSTRAINT candidate_max_80_chars CHECK (char_length(phrase) BETWEEN 1 AND 80),
    dedupe_key          text        NOT NULL,   -- normalised phrase (lowercased, ws-collapsed, apostrophe-normalised)
    source_kind         text        NOT NULL CHECK (
        source_kind IN ('mined', 'telemetry_gap', 'terminology', 'generated', 'authored')),
    source_ref          text        NOT NULL,   -- dataset id + version + sha, or mining run id
    generation_batch_id uuid,
    tier                smallint    CHECK (tier IS NULL OR tier BETWEEN 1 AND 3),
    risk_flags          text[]      NOT NULL DEFAULT '{}',
    review_state        text        NOT NULL DEFAULT 'candidate' CHECK (
        review_state IN ('candidate', 'accepted', 'rejected', 'promoted')),
    reviewed_by         uuid,
    reviewed_at         timestamptz,
    review_engine       text,       -- 'human' | 'jury:<model>:<prompt_version>'
    jury_votes          jsonb       NOT NULL DEFAULT '[]'::jsonb,
    validator_report    jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- frequency, distinct_authors, checks
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    -- Risk-flagged candidates are human-mandatory (ADR-0043 §6).
    CONSTRAINT candidates_risk_tier_chk CHECK (cardinality(risk_flags) = 0 OR tier IS NULL OR tier = 3)
);

-- One candidate per normalised phrase per scope+language; re-mining the same
-- phrase is an upsert into validator_report, not a duplicate row.
CREATE UNIQUE INDEX corpus_candidates_dedupe_uq
    ON corpus_candidates (
        COALESCE(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid),
        language, dedupe_key);

CREATE INDEX corpus_candidates_state_idx ON corpus_candidates (review_state, tier);
CREATE INDEX corpus_candidates_kind_idx ON corpus_candidates (source_kind);
CREATE INDEX corpus_candidates_batch_idx ON corpus_candidates (generation_batch_id)
    WHERE generation_batch_id IS NOT NULL;

GRANT SELECT, INSERT, UPDATE ON corpus_candidates TO app_role;
GRANT SELECT, INSERT, UPDATE ON corpus_candidates TO tenant_writer;

ALTER TABLE corpus_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE corpus_candidates FORCE ROW LEVEL SECURITY;

-- Global candidates visible to every tenant context; tenant candidates only
-- to their tenant (autocomplete_phrases 'tenant_visibility' pattern).
CREATE POLICY candidates_visibility ON corpus_candidates
    FOR SELECT TO app_role
    USING (tenant_id IS NULL OR tenant_id = current_setting('app.tenant_id', true)::uuid);

-- app_role may only write rows in its own tenant scope.
CREATE POLICY candidates_app_insert ON corpus_candidates
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY candidates_app_update ON corpus_candidates
    FOR UPDATE TO app_role
    USING      (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- corpus-forge (operator CLI) runs as tenant_writer and owns the global rows
-- (seed_system_rows pattern from migration 0023).
CREATE POLICY candidates_forge_select ON corpus_candidates
    FOR SELECT TO tenant_writer
    USING (true);

CREATE POLICY candidates_forge_insert ON corpus_candidates
    FOR INSERT TO tenant_writer
    WITH CHECK (tenant_id IS NULL);

CREATE POLICY candidates_forge_update ON corpus_candidates
    FOR UPDATE TO tenant_writer
    USING (tenant_id IS NULL)
    WITH CHECK (tenant_id IS NULL);

-- Candidates are never deleted (rejects are the calibration set).
CREATE POLICY candidates_delete_forbidden ON corpus_candidates
    FOR DELETE TO app_role, tenant_writer
    USING (false);

COMMIT;
