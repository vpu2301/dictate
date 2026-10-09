-- 0097: the data register and speaker consents — corpus-v3 Epic F.
--
-- WHAT AN AUDITOR ASKS, IN ORDER: which datasets does this system use to
-- measure itself, where did they come from, whose voices are in them, on
-- what basis, which versions are frozen, and can I have that as a document.
-- Today every one of those answers exists somewhere — a snapshot row, an
-- import journal, a git tag, somebody's memory — and assembling them is a
-- half-day of archaeology per question. This migration is the place they
-- are all answerable from.
--
-- IT IS AN ENGINEERING TOOL, NOT A LEGAL OPINION. Nothing here classifies
-- the system under the EU AI Act; see docs/compliance/data-register-ai-act.md
-- for what the register does and does not claim.
--
-- WHY contains_patient_data IS A CHECK AND NOT A FLAG. The corpus is
-- scripted synthetic speech, and "no patient data in the eval corpus" is a
-- policy enforced at three doors already (the scripted-only recorder, the
-- PII sweep, the ad-hoc attestation). A register that could RECORD a
-- patient-data set would be a register that expects one to exist. The
-- constraint states the policy in the one place that cannot be forgotten;
-- if the policy ever changes, changing it is a migration somebody reviews.
--
-- WHY contains_personal_data IS NOT THE SAME QUESTION. It is usually TRUE.
-- The corpus contains no patient data and it does contain recordings of
-- identifiable employees reading scripts — a voice is personal data under
-- the GDPR whatever the words are. Conflating the two is the single most
-- common way a register like this becomes wrong, so they are two columns
-- and the second one defaults to the honest answer.
--
-- WHY CONSENT IS A TABLE AND REVOCATION IS A COLUMN. "Did this speaker
-- consent" is answerable at any point in time, not just now: a snapshot
-- published in March was lawful on the basis that existed in March, and a
-- revocation in August does not retroactively unmake it. So consents are
-- rows with a grant and a revocation instant, at most one active per
-- (speaker, scope), and exclusion is DERIVED — future measurements skip the
-- speaker's takes, past runs stay exactly as they were. That is also why
-- revocation deletes nothing here: erasure of the audio is the separate,
-- deliberate act the privacy runbook describes.

BEGIN;

-- ── speaker consents ──────────────────────────────────────────────────

CREATE TABLE corpus_speaker_consents (
    id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  uuid        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- users.sub, soft ref (0089 pattern). The person whose voice it is.
    speaker_id uuid        NOT NULL,
    -- One scope today; named rather than implied because a second one
    -- ("голос у демонстраційних матеріалах") is a different permission and
    -- must not be granted by accident along with this one.
    scope      text        NOT NULL DEFAULT 'corpus_voice'
        CONSTRAINT speaker_consent_scope_chk CHECK (scope IN ('corpus_voice')),
    granted_at timestamptz NOT NULL DEFAULT now(),
    granted_by uuid,
    revoked_at timestamptz,
    revoked_by uuid,
    -- Free-text record of HOW consent was obtained, for the auditor who
    -- asks. Not a substitute for the paper; a pointer to it.
    note       text        CHECK (note IS NULL OR char_length(note) <= 500),
    CONSTRAINT speaker_consent_revocation_chk
        CHECK (revoked_at IS NULL OR revoked_at >= granted_at)
);

-- At most one ACTIVE consent per speaker and scope; revoked rows accumulate
-- as history, and a re-grant is a new row rather than a resurrection.
CREATE UNIQUE INDEX corpus_speaker_consents_active_idx
    ON corpus_speaker_consents (tenant_id, speaker_id, scope)
    WHERE revoked_at IS NULL;

CREATE INDEX corpus_speaker_consents_tenant_idx
    ON corpus_speaker_consents (tenant_id, granted_at DESC);

GRANT SELECT, INSERT, UPDATE ON corpus_speaker_consents TO app_role;
ALTER TABLE corpus_speaker_consents ENABLE ROW LEVEL SECURITY;
ALTER TABLE corpus_speaker_consents FORCE ROW LEVEL SECURITY;
CREATE POLICY speaker_consents_tenant_all ON corpus_speaker_consents
    FOR ALL TO app_role
    USING      (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- ── the dataset register ──────────────────────────────────────────────

CREATE TABLE dataset_registry (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name        text        NOT NULL CHECK (char_length(name) BETWEEN 1 AND 120),
    version     text        NOT NULL CHECK (char_length(version) BETWEEN 1 AND 40),
    -- Already computed everywhere it matters: a snapshot's manifest digest,
    -- an import's file digest. Re-used, never recomputed — two digests for
    -- one artefact is one digest too many.
    sha256      text        NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    purpose     text        NOT NULL CHECK (char_length(purpose) BETWEEN 1 AND 300),
    data_origin text        NOT NULL
        CONSTRAINT dataset_origin_chk
        CHECK (data_origin IN ('synthetic_scripted', 'derived', 'external')),
    -- Always false. See the header: this is the policy, stated where it
    -- cannot be forgotten.
    contains_patient_data  boolean NOT NULL DEFAULT false
        CONSTRAINT dataset_no_patient_data_chk CHECK (contains_patient_data = false),
    -- Usually true: employee voices are personal data whatever the script
    -- says. A different question from the one above.
    contains_personal_data boolean NOT NULL,
    -- users.sub of everyone whose voice is in this dataset.
    speakers    uuid[]      NOT NULL DEFAULT '{}',
    legal_basis text        NOT NULL CHECK (char_length(legal_basis) BETWEEN 1 AND 200),
    retention_period text   NOT NULL CHECK (char_length(retention_period) BETWEEN 1 AND 120),
    storage_location text   NOT NULL CHECK (char_length(storage_location) BETWEEN 1 AND 200),
    -- Frozen = the artefact will not change again. A snapshot is frozen by
    -- construction; a CSV import describes a file that already exists.
    frozen      boolean     NOT NULL DEFAULT false,
    -- Which pipeline artefact produced this entry, so the register can be
    -- refreshed idempotently instead of accumulating near-duplicates.
    source_kind text        NOT NULL
        CONSTRAINT dataset_source_kind_chk
        CHECK (source_kind IN ('corpus_snapshot', 'csv_import', 'manual')),
    source_id   uuid,
    utterances  integer     CHECK (utterances IS NULL OR utterances >= 0),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT dataset_registry_identity UNIQUE (tenant_id, name, version)
);

CREATE INDEX dataset_registry_tenant_idx
    ON dataset_registry (tenant_id, created_at DESC);
CREATE UNIQUE INDEX dataset_registry_source_idx
    ON dataset_registry (tenant_id, source_kind, source_id)
    WHERE source_id IS NOT NULL;

GRANT SELECT, INSERT, UPDATE ON dataset_registry TO app_role;
ALTER TABLE dataset_registry ENABLE ROW LEVEL SECURITY;
ALTER TABLE dataset_registry FORCE ROW LEVEL SECURITY;
CREATE POLICY dataset_registry_tenant_all ON dataset_registry
    FOR ALL TO app_role
    USING      (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

COMMIT;
