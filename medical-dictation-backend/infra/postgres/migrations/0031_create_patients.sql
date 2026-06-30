-- Sprint 11 / M2 — patients & the per-patient clinical record.
--
-- This migration introduces the clinical/EHR "core" data owned by
-- core-service (port 8003): the patient roster plus the records that hang
-- off a patient — encounters, clinical notes, consents, anamnesis and
-- privacy (DSAR / erasure) requests.
--
-- The `reports` table (migration 0016) already carries a nullable
-- `patient_id` reserved for "sprint 11"; it is intentionally NOT promoted
-- to a FK here. report-service owns `reports` and core-service owns
-- `patients`; a cross-service FK would couple their migration order and
-- their RLS. The link stays a soft reference, scoped by the shared
-- tenant_id under RLS.
--
-- Patient demographics (names, DOB, MRN) are stored as plaintext columns,
-- protected by Postgres RLS + FORCE exactly like `users` (emails) and
-- `reports` (titles). The platform's envelope-encryption-at-rest applies to
-- audio/transcript BLOBS in object storage (ADR-0009/0010), not to
-- relational PII columns.

-- ── patients ────────────────────────────────────────────────────────

CREATE TABLE patients (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,

    -- Bilingual display names. UA is the primary; EN is optional and falls
    -- back to UA at the service layer when blank.
    name_uk         TEXT NOT NULL,
    name_en         TEXT NOT NULL DEFAULT '',

    dob             DATE,
    sex             TEXT NOT NULL DEFAULT 'U'
                        CHECK (sex IN ('M', 'F', 'U')),

    -- Medical record number. Free-form per tenant; unique when present.
    mrn             TEXT NOT NULL DEFAULT '',

    -- Chief complaint / one-line summary, bilingual.
    summary_uk      TEXT NOT NULL DEFAULT '',
    summary_en      TEXT NOT NULL DEFAULT '',

    -- Conditions / free tags shown as chips in the roster.
    tags            TEXT[] NOT NULL DEFAULT '{}',

    status          TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'inactive', 'deceased')),

    -- Denormalised last-activity timestamp, bumped by the service layer on
    -- encounter / note writes. Drives the roster "Last visit" column and the
    -- default sort.
    last_visit_at   TIMESTAMPTZ,

    created_by      UUID NOT NULL REFERENCES users(sub) ON DELETE RESTRICT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Type-ahead search over names + MRN.
    search_vector   tsvector GENERATED ALWAYS AS (
                        to_tsvector(
                            'simple',
                            coalesce(name_uk, '') || ' ' ||
                            coalesce(name_en, '') || ' ' ||
                            coalesce(mrn, '')
                        )
                    ) STORED
);

-- MRN is unique within a tenant when supplied (empty string = not assigned).
CREATE UNIQUE INDEX patients_mrn_per_tenant_unique
    ON patients (tenant_id, mrn) WHERE mrn <> '';

CREATE INDEX patients_tenant_recent_idx
    ON patients (tenant_id, last_visit_at DESC NULLS LAST, id);
CREATE INDEX patients_search_vector_idx
    ON patients USING gin (search_vector);
CREATE INDEX patients_tags_idx
    ON patients USING gin (tags);

ALTER TABLE patients ENABLE ROW LEVEL SECURITY;
ALTER TABLE patients FORCE  ROW LEVEL SECURITY;

CREATE POLICY patients_tenant_select ON patients
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY patients_tenant_insert ON patients
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY patients_tenant_update ON patients
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY patients_tenant_delete ON patients
    FOR DELETE TO app_role
    USING (false);  -- soft-delete via status; hard delete forbidden
CREATE POLICY patients_tenant_restrictive ON patients
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE ON patients TO app_role;

-- ── encounters ──────────────────────────────────────────────────────

CREATE TABLE encounters (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    patient_id      UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,

    kind            TEXT NOT NULL DEFAULT 'visit'
                        CHECK (kind IN ('visit', 'phone', 'video',
                                        'scribe', 'followup', 'other')),
    reason          TEXT NOT NULL DEFAULT '',
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    status          TEXT NOT NULL DEFAULT 'completed'
                        CHECK (status IN ('scheduled', 'in_progress',
                                          'completed', 'cancelled')),

    created_by      UUID NOT NULL REFERENCES users(sub) ON DELETE RESTRICT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX encounters_patient_idx
    ON encounters (tenant_id, patient_id, occurred_at DESC);
CREATE INDEX encounters_schedule_idx
    ON encounters (tenant_id, occurred_at) WHERE status = 'scheduled';

ALTER TABLE encounters ENABLE ROW LEVEL SECURITY;
ALTER TABLE encounters FORCE  ROW LEVEL SECURITY;

CREATE POLICY encounters_tenant_select ON encounters
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY encounters_tenant_insert ON encounters
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY encounters_tenant_update ON encounters
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY encounters_tenant_delete ON encounters
    FOR DELETE TO app_role
    USING (false);
CREATE POLICY encounters_tenant_restrictive ON encounters
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE ON encounters TO app_role;

-- ── clinical_notes ──────────────────────────────────────────────────

CREATE TABLE clinical_notes (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    patient_id        UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    encounter_id      UUID REFERENCES encounters(id) ON DELETE SET NULL,

    -- SOAP / APSO / DAP / free — the section scaffolding lives in `sections`.
    structure         TEXT NOT NULL DEFAULT 'soap'
                          CHECK (structure IN ('soap', 'apso', 'dap', 'free')),
    title             TEXT NOT NULL DEFAULT '',
    sections          JSONB NOT NULL DEFAULT '[]'::jsonb,

    status            TEXT NOT NULL DEFAULT 'draft'
                          CHECK (status IN ('draft', 'signed')),

    author_id         UUID NOT NULL REFERENCES users(sub) ON DELETE RESTRICT,
    source_session_id UUID,            -- scribe session that produced the note

    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    signed_at         TIMESTAMPTZ,

    CONSTRAINT clinical_notes_signed_has_ts
        CHECK ((status = 'signed') = (signed_at IS NOT NULL))
);

CREATE INDEX clinical_notes_patient_idx
    ON clinical_notes (tenant_id, patient_id, created_at DESC);
CREATE INDEX clinical_notes_feed_idx
    ON clinical_notes (tenant_id, status, updated_at DESC, id);

ALTER TABLE clinical_notes ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinical_notes FORCE  ROW LEVEL SECURITY;

CREATE POLICY clinical_notes_tenant_select ON clinical_notes
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY clinical_notes_tenant_insert ON clinical_notes
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY clinical_notes_tenant_update ON clinical_notes
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY clinical_notes_tenant_delete ON clinical_notes
    FOR DELETE TO app_role
    USING (false);
CREATE POLICY clinical_notes_tenant_restrictive ON clinical_notes
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE ON clinical_notes TO app_role;

-- ── patient_consents ────────────────────────────────────────────────

CREATE TABLE patient_consents (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    patient_id      UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    encounter_id    UUID REFERENCES encounters(id) ON DELETE SET NULL,

    type            TEXT NOT NULL DEFAULT 'ai_scribe',   -- ai_scribe | data_processing | …
    method          TEXT NOT NULL DEFAULT 'verbal',      -- verbal | written | digital
    version         TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'granted'
                        CHECK (status IN ('granted', 'withdrawn')),

    granted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    withdrawn_at    TIMESTAMPTZ,
    created_by      UUID NOT NULL REFERENCES users(sub) ON DELETE RESTRICT,

    CONSTRAINT patient_consents_withdrawn_has_ts
        CHECK ((status = 'withdrawn') = (withdrawn_at IS NOT NULL))
);

CREATE INDEX patient_consents_patient_idx
    ON patient_consents (tenant_id, patient_id, granted_at DESC);

ALTER TABLE patient_consents ENABLE ROW LEVEL SECURITY;
ALTER TABLE patient_consents FORCE  ROW LEVEL SECURITY;

CREATE POLICY patient_consents_tenant_select ON patient_consents
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY patient_consents_tenant_insert ON patient_consents
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY patient_consents_tenant_update ON patient_consents
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY patient_consents_tenant_delete ON patient_consents
    FOR DELETE TO app_role
    USING (false);
CREATE POLICY patient_consents_tenant_restrictive ON patient_consents
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE ON patient_consents TO app_role;

-- ── patient_anamnesis (1:1 with patient) ────────────────────────────

CREATE TABLE patient_anamnesis (
    patient_id      UUID PRIMARY KEY REFERENCES patients(id) ON DELETE CASCADE,
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,

    -- Structured history: chief complaint, medications, allergies,
    -- conditions, family/social history. Shape owned by the service layer.
    record          JSONB NOT NULL DEFAULT '{}'::jsonb,

    updated_by      UUID NOT NULL REFERENCES users(sub) ON DELETE RESTRICT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE patient_anamnesis ENABLE ROW LEVEL SECURITY;
ALTER TABLE patient_anamnesis FORCE  ROW LEVEL SECURITY;

CREATE POLICY patient_anamnesis_tenant_select ON patient_anamnesis
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY patient_anamnesis_tenant_insert ON patient_anamnesis
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY patient_anamnesis_tenant_update ON patient_anamnesis
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY patient_anamnesis_tenant_delete ON patient_anamnesis
    FOR DELETE TO app_role
    USING (false);
CREATE POLICY patient_anamnesis_tenant_restrictive ON patient_anamnesis
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE ON patient_anamnesis TO app_role;

-- ── patient_privacy_requests (DSAR / erasure log) ───────────────────

CREATE TABLE patient_privacy_requests (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    patient_id      UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,

    kind            TEXT NOT NULL CHECK (kind IN ('dsar', 'erasure')),
    reason          TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'scheduled',
                                          'completed', 'cancelled')),

    requested_by    UUID NOT NULL REFERENCES users(sub) ON DELETE RESTRICT,
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    scheduled_for   TIMESTAMPTZ      -- erasure grace-period target
);

CREATE INDEX patient_privacy_requests_patient_idx
    ON patient_privacy_requests (tenant_id, patient_id, requested_at DESC);

ALTER TABLE patient_privacy_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE patient_privacy_requests FORCE  ROW LEVEL SECURITY;

CREATE POLICY patient_privacy_requests_tenant_select ON patient_privacy_requests
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY patient_privacy_requests_tenant_insert ON patient_privacy_requests
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY patient_privacy_requests_tenant_update ON patient_privacy_requests
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY patient_privacy_requests_tenant_delete ON patient_privacy_requests
    FOR DELETE TO app_role
    USING (false);
CREATE POLICY patient_privacy_requests_tenant_restrictive ON patient_privacy_requests
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE ON patient_privacy_requests TO app_role;
