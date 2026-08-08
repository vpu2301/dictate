-- 0067_evidence_domain.sql
-- EVA-S01: evidence domain schema (documents/chunks/corpus snapshots, questions/
-- answers/segments, append-only answer_provenance, traces, followups, checks).
--
-- Conventions (per 0063/0065): every table RLS + FORCE, at least one PERMISSIVE
-- policy per allowed command, one RESTRICTIVE catch-all, tenant comparison via
-- current_setting('app.tenant_id', true)::uuid. questions/answers additionally
-- carry a user-private read policy on current_setting('app.user_id', true)
-- (platform GUC precedent: autocomplete-service; the sprint spec's app.user_sub
-- was renamed to the existing GUC — recorded as an as-built delta).
-- answer_provenance is append-only: grants + immutability trigger, per the
-- audit.events pattern (audit.events_immutable is column-coupled, so evidence
-- gets its own trigger fn).

-- ---------------------------------------------------------------- roles

-- New realm role for corpus curation (spec §4). users.role carried a closed
-- CHECK since 0002; extend it. (Postgres default name for the inline CHECK.)
ALTER TABLE users DROP CONSTRAINT users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN
    ('tenant_admin', 'clinician', 'nurse', 'auditor', 'service', 'knowledge_admin'));

-- ---------------------------------------------------------------- corpus

CREATE TABLE documents (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL,
    canonical_id      text NOT NULL,
    title             text NOT NULL,
    source_authority  text NOT NULL CHECK (source_authority IN
                        ('tenant', 'national', 'international', 'primary_literature', 'other')),
    evidence_tier     text NOT NULL CHECK (evidence_tier IN
                        ('guideline', 'systematic_review', 'rct', 'observational', 'other')),
    jurisdiction      text,
    specialty         text[] NOT NULL DEFAULT '{}',
    published_at      date,
    valid_until       date,
    license_class     text NOT NULL CHECK (license_class IN
                        ('public_domain', 'open_license', 'licensed_redistributable',
                         'licensed_internal', 'restricted')),
    retracted         boolean NOT NULL DEFAULT false,
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, canonical_id)
);

CREATE INDEX documents_authority_idx
    ON documents (tenant_id, source_authority, published_at);

CREATE TABLE document_versions (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid NOT NULL,
    document_id  uuid NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    version      integer NOT NULL CHECK (version >= 1),
    content_ref  text NOT NULL,
    parsed_ref   text,
    checksum     text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, version)
);

CREATE TABLE chunks (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            uuid NOT NULL,
    document_version_id  uuid NOT NULL REFERENCES document_versions (id) ON DELETE CASCADE,
    section_path         text,
    char_start           integer NOT NULL CHECK (char_start >= 0),
    char_end             integer NOT NULL CHECK (char_end > char_start),
    text                 text NOT NULL,
    embedding            vector(1024),
    created_at           timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX chunks_document_version_idx ON chunks (tenant_id, document_version_id);
-- HNSW over cosine distance; empty-table build is cheap, S02 fills it.
CREATE INDEX chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE corpus_snapshots (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid NOT NULL,
    label            text NOT NULL,
    member_versions  uuid[] NOT NULL DEFAULT '{}',
    frozen_at        timestamptz NOT NULL DEFAULT now(),
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, label)
);

-- ---------------------------------------------------------------- Q&A

CREATE TABLE questions (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL,
    user_sub    uuid NOT NULL,
    text        text NOT NULL,
    mode        text NOT NULL CHECK (mode IN
                  ('quick_search', 'contextual', 'deeptrace', 'drug')),
    locale      text NOT NULL DEFAULT 'uk',
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX questions_user_idx ON questions (tenant_id, user_sub, created_at DESC);

CREATE TABLE answers (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL,
    user_sub            uuid NOT NULL,
    question_id         uuid NOT NULL REFERENCES questions (id) ON DELETE CASCADE,
    status              text NOT NULL CHECK (status IN ('ok', 'insufficient_basis', 'deflected')),
    mode                text NOT NULL CHECK (mode IN
                          ('quick_search', 'contextual', 'deeptrace', 'drug')),
    verified            boolean NOT NULL DEFAULT false,
    -- EncryptedObjectStore key of the full AnswerEnvelope (may echo PHI).
    envelope_ref        text NOT NULL,
    previous_answer_id  uuid REFERENCES answers (id),
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX answers_question_idx ON answers (tenant_id, question_id);

CREATE TABLE answer_segments (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          uuid NOT NULL,
    answer_id          uuid NOT NULL REFERENCES answers (id) ON DELETE CASCADE,
    ord                integer NOT NULL CHECK (ord >= 0),
    kind               text NOT NULL CHECK (kind IN
                         ('evidence', 'patient_fact', 'interpretation', 'uncertainty',
                          'missing_info', 'next_step')),
    text               text NOT NULL,
    strength           text CHECK (strength IN
                         ('guideline', 'systematic_review', 'rct', 'observational', 'other')),
    citations          jsonb NOT NULL DEFAULT '[]',
    patient_fact_refs  jsonb NOT NULL DEFAULT '[]',
    created_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (answer_id, ord),
    -- ET2 at the storage layer too: evidence rows must carry citations.
    CONSTRAINT answer_segments_et2_citations CHECK
        (kind <> 'evidence' OR jsonb_array_length(citations) >= 1)
);

CREATE INDEX answer_segments_answer_idx ON answer_segments (tenant_id, answer_id, ord);

CREATE TABLE answer_provenance (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL,
    -- Deliberately NOT an FK: provenance is append-only and must survive
    -- erasure of the answer row (pseudonymization, rule PR1) — like audit
    -- events referencing deleted targets.
    answer_id           uuid NOT NULL,
    question_ref        text NOT NULL,
    snapshot_hash       text,
    consumed_fields     jsonb NOT NULL DEFAULT '[]',
    passage_ids         jsonb NOT NULL DEFAULT '[]',
    web_refs            jsonb NOT NULL DEFAULT '[]',
    connectors          jsonb NOT NULL DEFAULT '[]',
    corpus_snapshot_id  uuid REFERENCES corpus_snapshots (id),
    model_pins          jsonb NOT NULL DEFAULT '{}',
    prompt_versions     jsonb NOT NULL DEFAULT '{}',
    pipeline_version    text NOT NULL,
    build_version       text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (answer_id)
);

CREATE OR REPLACE FUNCTION evidence_provenance_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'answer_provenance is append-only (operation=%, id=%, tenant=%)',
        TG_OP, COALESCE(OLD.id::text, NEW.id::text), COALESCE(OLD.tenant_id::text, NEW.tenant_id::text)
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER answer_provenance_no_update
    BEFORE UPDATE ON answer_provenance
    FOR EACH ROW EXECUTE FUNCTION evidence_provenance_immutable();

CREATE TRIGGER answer_provenance_no_delete
    BEFORE DELETE ON answer_provenance
    FOR EACH ROW EXECUTE FUNCTION evidence_provenance_immutable();

CREATE TABLE answer_traces (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL,
    answer_id   uuid NOT NULL REFERENCES answers (id) ON DELETE CASCADE,
    stage       text NOT NULL,
    started_at  timestamptz NOT NULL,
    ended_at    timestamptz NOT NULL,
    outcome     text NOT NULL CHECK (outcome IN ('ok', 'retried', 'failed', 'skipped')),
    meta        jsonb NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now()
    -- ops retention: 90 days, enforced by a reaper job (S12), not by the schema
);

CREATE INDEX answer_traces_answer_idx ON answer_traces (tenant_id, answer_id);

CREATE TABLE followups (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL,
    answer_id         uuid NOT NULL REFERENCES answers (id) ON DELETE CASCADE,
    payload           jsonb NOT NULL,
    answered_at       timestamptz,
    answer_value_ref  text,
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX followups_answer_idx ON followups (tenant_id, answer_id);

CREATE TABLE checks_results (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL,
    answer_id   uuid NOT NULL REFERENCES answers (id) ON DELETE CASCADE,
    rule_id     text NOT NULL,
    engine      text NOT NULL,
    severity    text NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    entities    jsonb NOT NULL DEFAULT '[]',
    db_version  text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX checks_results_answer_idx ON checks_results (tenant_id, answer_id);

-- ---------------------------------------------------------------- RLS

-- Corpus tables: tenant-scoped CRUD for app_role; documents may be updated
-- (retraction/valid_until on corpus refresh), versions are insert-only rows.

ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE  ROW LEVEL SECURITY;
CREATE POLICY documents_tenant_select ON documents
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY documents_tenant_insert ON documents
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY documents_tenant_update ON documents
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY documents_tenant_delete ON documents
    FOR DELETE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY documents_tenant_restrictive ON documents
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON documents TO app_role;

ALTER TABLE document_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_versions FORCE  ROW LEVEL SECURITY;
CREATE POLICY document_versions_tenant_select ON document_versions
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY document_versions_tenant_insert ON document_versions
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY document_versions_tenant_delete ON document_versions
    FOR DELETE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY document_versions_tenant_restrictive ON document_versions
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT, INSERT, DELETE ON document_versions TO app_role;

ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks FORCE  ROW LEVEL SECURITY;
CREATE POLICY chunks_tenant_select ON chunks
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY chunks_tenant_insert ON chunks
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY chunks_tenant_delete ON chunks
    FOR DELETE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY chunks_tenant_restrictive ON chunks
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT, INSERT, DELETE ON chunks TO app_role;

-- corpus_snapshots: immutable by grant (no UPDATE, no DELETE).
ALTER TABLE corpus_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE corpus_snapshots FORCE  ROW LEVEL SECURITY;
CREATE POLICY corpus_snapshots_tenant_select ON corpus_snapshots
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY corpus_snapshots_tenant_insert ON corpus_snapshots
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY corpus_snapshots_tenant_restrictive ON corpus_snapshots
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT, INSERT ON corpus_snapshots TO app_role;

-- questions/answers: dual-key — tenant AND owning user (documented deviation:
-- user-private read model; GUC app.user_id per platform precedent).

ALTER TABLE questions ENABLE ROW LEVEL SECURITY;
ALTER TABLE questions FORCE  ROW LEVEL SECURITY;
CREATE POLICY questions_owner_select ON questions
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid
           AND user_sub = current_setting('app.user_id', true)::uuid);
CREATE POLICY questions_owner_insert ON questions
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid
                AND user_sub = current_setting('app.user_id', true)::uuid);
CREATE POLICY questions_owner_delete ON questions
    FOR DELETE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid
           AND user_sub = current_setting('app.user_id', true)::uuid);
CREATE POLICY questions_tenant_restrictive ON questions
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT, INSERT, DELETE ON questions TO app_role;

ALTER TABLE answers ENABLE ROW LEVEL SECURITY;
ALTER TABLE answers FORCE  ROW LEVEL SECURITY;
CREATE POLICY answers_owner_select ON answers
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid
           AND user_sub = current_setting('app.user_id', true)::uuid);
CREATE POLICY answers_owner_insert ON answers
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid
                AND user_sub = current_setting('app.user_id', true)::uuid);
CREATE POLICY answers_owner_delete ON answers
    FOR DELETE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid
           AND user_sub = current_setting('app.user_id', true)::uuid);
CREATE POLICY answers_tenant_restrictive ON answers
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT, INSERT, DELETE ON answers TO app_role;

-- Pipeline-written child tables: tenant-scoped; segments/traces/checks are
-- insert-only for app_role, followups may be updated (answered_at).

ALTER TABLE answer_segments ENABLE ROW LEVEL SECURITY;
ALTER TABLE answer_segments FORCE  ROW LEVEL SECURITY;
CREATE POLICY answer_segments_tenant_select ON answer_segments
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY answer_segments_tenant_insert ON answer_segments
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY answer_segments_tenant_delete ON answer_segments
    FOR DELETE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY answer_segments_tenant_restrictive ON answer_segments
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT, INSERT, DELETE ON answer_segments TO app_role;

ALTER TABLE answer_provenance ENABLE ROW LEVEL SECURITY;
ALTER TABLE answer_provenance FORCE  ROW LEVEL SECURITY;
CREATE POLICY answer_provenance_tenant_select ON answer_provenance
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY answer_provenance_tenant_insert ON answer_provenance
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY answer_provenance_tenant_restrictive ON answer_provenance
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
-- Append-only: SELECT + INSERT only; UPDATE/DELETE die on the trigger anyway.
GRANT SELECT, INSERT ON answer_provenance TO app_role;

ALTER TABLE answer_traces ENABLE ROW LEVEL SECURITY;
ALTER TABLE answer_traces FORCE  ROW LEVEL SECURITY;
CREATE POLICY answer_traces_tenant_select ON answer_traces
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY answer_traces_tenant_insert ON answer_traces
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY answer_traces_tenant_delete ON answer_traces
    FOR DELETE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY answer_traces_tenant_restrictive ON answer_traces
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT, INSERT, DELETE ON answer_traces TO app_role;

ALTER TABLE followups ENABLE ROW LEVEL SECURITY;
ALTER TABLE followups FORCE  ROW LEVEL SECURITY;
CREATE POLICY followups_tenant_select ON followups
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY followups_tenant_insert ON followups
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY followups_tenant_update ON followups
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY followups_tenant_delete ON followups
    FOR DELETE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY followups_tenant_restrictive ON followups
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON followups TO app_role;

ALTER TABLE checks_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE checks_results FORCE  ROW LEVEL SECURITY;
CREATE POLICY checks_results_tenant_select ON checks_results
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY checks_results_tenant_insert ON checks_results
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY checks_results_tenant_delete ON checks_results
    FOR DELETE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY checks_results_tenant_restrictive ON checks_results
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT, INSERT, DELETE ON checks_results TO app_role;

-- ---------------------------------------------------------------- erasure

-- GDPR erasure fan-out (patient/user data only; the corpus is not personal
-- data, answer_provenance is preserved via pseudonymization per rule PR1).
GRANT SELECT, DELETE ON questions, answers, answer_segments, answer_traces,
    followups, checks_results TO mdx_erasure;

CREATE POLICY questions_erasure_select ON questions
    FOR SELECT TO mdx_erasure
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY questions_erasure_delete ON questions
    FOR DELETE TO mdx_erasure
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY answers_erasure_select ON answers
    FOR SELECT TO mdx_erasure
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY answers_erasure_delete ON answers
    FOR DELETE TO mdx_erasure
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY answer_segments_erasure_select ON answer_segments
    FOR SELECT TO mdx_erasure
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY answer_segments_erasure_delete ON answer_segments
    FOR DELETE TO mdx_erasure
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY answer_traces_erasure_select ON answer_traces
    FOR SELECT TO mdx_erasure
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY answer_traces_erasure_delete ON answer_traces
    FOR DELETE TO mdx_erasure
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY followups_erasure_select ON followups
    FOR SELECT TO mdx_erasure
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY followups_erasure_delete ON followups
    FOR DELETE TO mdx_erasure
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY checks_results_erasure_select ON checks_results
    FOR SELECT TO mdx_erasure
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY checks_results_erasure_delete ON checks_results
    FOR DELETE TO mdx_erasure
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
