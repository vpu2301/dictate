-- 0054: ICD-10 (МКХ-10) reference table — sprint 13 step 03.
--
-- Global read-only clinical reference: makes `structured_diagnosis`
-- real (search endpoint + extractor proposals). Zero tenant/patient
-- data ⇒ NO RLS, following the 0008 medical_prompts / 0011
-- voice_commands precedent; registered in the check-rls allowlist
-- (scripts/ci/check-rls-policies.py) with an ADR-0021 amendment note.
--
-- Loaded by scripts/load-icd10.py (idempotent upsert). The committed
-- fixture (infra/seeds/icd10/fixture.csv) covers CI/dev; the FULL
-- МКХ-10-АМ table is an ops step — see docs/runbooks/icd10.md.

BEGIN;

CREATE TABLE icd10_codes (
    code          TEXT PRIMARY KEY
                  CHECK (code ~ '^[A-Z][0-9]{2}(\.[0-9A-Z]{1,4})?$'),
    display_uk    TEXT NOT NULL CHECK (length(display_uk) > 0),
    display_en    TEXT NOT NULL DEFAULT '',
    parent_code   TEXT REFERENCES icd10_codes(code),
    chapter       TEXT NOT NULL DEFAULT '',
    is_leaf       BOOLEAN NOT NULL DEFAULT true,  -- selectable vs heading
    search_vector tsvector GENERATED ALWAYS AS (
        to_tsvector('simple', coalesce(display_uk, '') || ' ' ||
                              coalesce(display_en, '') || ' ' || code)
    ) STORED
);

COMMENT ON TABLE icd10_codes IS
    'МКХ-10 reference (sprint 13). Global, read-only for services; '
    'no RLS by design — see check-rls allowlist + ADR-0021 amendment.';

-- FTS over displays + code (simple config per ADR-0021 — no uk stemmer).
CREATE INDEX idx_icd10_fts ON icd10_codes USING GIN (search_vector);
-- Prefix typing path ("I1" → I10…) needs text_pattern_ops for LIKE 'I1%'.
CREATE INDEX idx_icd10_prefix ON icd10_codes (code text_pattern_ops);
-- Parent lookups (children of a heading).
CREATE INDEX idx_icd10_parent ON icd10_codes (parent_code);

-- Read-only for services; the loader runs privileged (tenant_writer,
-- same as reference seeding elsewhere). tenant_writer also needs SELECT:
-- the loader diffs existing rows to report inserted/updated/unchanged.
GRANT SELECT ON icd10_codes TO app_role, audit_reader, tenant_writer;
GRANT INSERT, UPDATE, DELETE ON icd10_codes TO tenant_writer;

COMMIT;
