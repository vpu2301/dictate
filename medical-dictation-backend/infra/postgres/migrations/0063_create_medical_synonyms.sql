-- Sprint 15 (ADR-0038): medical_synonyms — the query-expansion dictionary.
--
-- ADR-0021 accepted `simple` FTS (no stemming, no synonyms) and named this
-- sprint for the synonym layer: rows group equivalent clinical terms
-- («ІМ» ↔ «інфаркт міокарда» ↔ "MI") under one group_id; search expands a
-- matched query lexeme to (term OR syn1 OR syn2) tsquery groups.
--
-- Two scopes (the autocomplete_phrases model, minus the user tier):
--   system → tenant_id NULL  (seeded, visible to every tenant, immutable
--                             to app_role; tenant_writer curates)
--   tenant → tenant_id set   (tenant_admin-curated via /v1/synonyms;
--                             admin UI lands sprint 17)
--
-- `lexemes` is the term pre-normalized through to_tsvector('simple', …)
-- AT WRITE TIME — query-time matching is a plain array-overlap (&&) on the
-- GIN index, and both sides are guaranteed the same normalization because
-- both run through the same Postgres config. NB this is deliberately NOT
-- the nlp-service `abbreviations_global` dictionary: that one rewrites
-- TRANSCRIPT text at dictation time (direction-aware, replay-deterministic);
-- this one broadens SEARCH recall at query time and is tenant-extendable.
--
-- RLS lessons paid for by 0023/0038/0039: at least one PERMISSIVE policy
-- per command (restrictive-only = deny-all), nil-uuid coalesce in the
-- unique index, and `tenant_writer` already exists (never CREATE ROLE it).

CREATE TABLE medical_synonyms (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID REFERENCES tenants(id) ON DELETE CASCADE,
    group_id    UUID NOT NULL,
    term        TEXT NOT NULL CHECK (length(btrim(term)) BETWEEN 1 AND 120),
    lexemes     TEXT[] NOT NULL CHECK (cardinality(lexemes) >= 1),
    language    TEXT NOT NULL CHECK (language IN ('uk', 'en')),
    source      TEXT NOT NULL CHECK (source IN ('system', 'tenant')),
    created_by  UUID,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT synonym_scope_shape CHECK (
        (source = 'system' AND tenant_id IS NULL)
     OR (source = 'tenant' AND tenant_id IS NOT NULL)
    )
);

-- Term unique within its group per scope (0039 nil-uuid idiom). The same
-- term MAY appear in two groups — expansion unions the alternatives.
CREATE UNIQUE INDEX medical_synonyms_unique_term_per_group
    ON medical_synonyms (
        coalesce(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid),
        group_id,
        lower(term),
        language
    );

-- Query-time lookup: WHERE lexemes && ARRAY[<query lexemes>].
CREATE INDEX medical_synonyms_lexemes_gin ON medical_synonyms USING gin (lexemes);
CREATE INDEX medical_synonyms_group_idx ON medical_synonyms (group_id);

ALTER TABLE medical_synonyms ENABLE ROW LEVEL SECURITY;
ALTER TABLE medical_synonyms FORCE ROW LEVEL SECURITY;

-- PERMISSIVE visibility: system rows for everyone, tenant rows for the
-- owning tenant.
CREATE POLICY synonyms_visibility ON medical_synonyms
    FOR SELECT TO app_role
    USING (
        source = 'system'
        OR tenant_id = current_setting('app.tenant_id', true)::uuid
    );

-- PERMISSIVE writes: app_role touches ONLY tenant-scope rows of its own
-- tenant (the synonym.write permission narrows this to tenant_admin at
-- the app layer). System rows are unreachable for every write command —
-- there is no PERMISSIVE policy granting them, which IS the guard.
CREATE POLICY synonyms_tenant_insert ON medical_synonyms
    FOR INSERT TO app_role
    WITH CHECK (
        source = 'tenant'
        AND tenant_id = current_setting('app.tenant_id', true)::uuid
    );
CREATE POLICY synonyms_tenant_update ON medical_synonyms
    FOR UPDATE TO app_role
    USING (source = 'tenant' AND tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (source = 'tenant' AND tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY synonyms_tenant_delete ON medical_synonyms
    FOR DELETE TO app_role
    USING (source = 'tenant' AND tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON medical_synonyms TO app_role;

-- Seed/curation role for system rows (0023 idiom; the role exists since 0002).
GRANT SELECT, INSERT, UPDATE, DELETE ON medical_synonyms TO tenant_writer;
CREATE POLICY synonyms_seed_select ON medical_synonyms
    FOR SELECT TO tenant_writer USING (true);
CREATE POLICY synonyms_seed_insert ON medical_synonyms
    FOR INSERT TO tenant_writer WITH CHECK (source = 'system');
CREATE POLICY synonyms_seed_update ON medical_synonyms
    FOR UPDATE TO tenant_writer USING (source = 'system') WITH CHECK (source = 'system');
CREATE POLICY synonyms_seed_delete ON medical_synonyms
    FOR DELETE TO tenant_writer USING (source = 'system');
