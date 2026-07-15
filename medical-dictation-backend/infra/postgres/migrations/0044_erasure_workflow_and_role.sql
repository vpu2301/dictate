-- S11 step 04 — two-person erasure workflow + the mdx_erasure role.
--
-- Two independent controls, both here, before any engine code exists:
--
--   PROCESS   — patient_privacy_requests becomes a real state machine
--               (requested → review → approved → executing → completed |
--               rejected) with the two-person rule enforced by CHECK:
--               the reviewer can never be the requester.
--   PRIVILEGE — app_role physically cannot DELETE PHI rows (unchanged);
--               the reserved grants land on `mdx_erasure` (LOGIN role,
--               bootstrapped in init.sql — existing dev volumes need
--               `make reset-db`, OBS-A1-05). Every mdx_erasure statement
--               is still tenant-RLS-bound: FORCE RLS applies to it like
--               any role, so it gets its own per-table policies below.
--
-- `report_of_execution` (consumed by step 07's engine) is the honest
-- record of what an erasure actually did:
--   { executed_at, destroyed: [{kind, id, detail}],
--     retained: [{kind, id, legal_basis}], operator, engine_version }

-- ── Workflow columns (extend, don't replace, the 0031 table) ───────
-- (`reason` already exists in 0031; requester column is `requested_by`.)

ALTER TABLE patient_privacy_requests
    ADD COLUMN reviewed_by         UUID REFERENCES users(sub),
    ADD COLUMN reviewed_at         TIMESTAMPTZ,
    ADD COLUMN rejection_reason    TEXT,
    ADD COLUMN executing_at        TIMESTAMPTZ,
    ADD COLUMN completed_at        TIMESTAMPTZ,
    ADD COLUMN report_of_execution JSONB,
    -- DSAR package (S11 step 06): the envelope-encrypted ZIP's object key
    -- while it exists, and the TTL-deletion stamp once the cleanup job
    -- removes it (download answers 410 package_expired afterwards).
    ADD COLUMN package_object_key  TEXT,
    ADD COLUMN package_deleted_at  TIMESTAMPTZ,
    -- Erasure failure recording (S11 step 07): an erasure execution error
    -- leaves the request in 'executing' (no failed state by design — the
    -- idempotent re-run IS the recovery) with the error preserved here.
    ADD COLUMN last_error          TEXT;

-- ── Status remap + per-kind state machine ───────────────────────────

ALTER TABLE patient_privacy_requests
    DROP CONSTRAINT patient_privacy_requests_status_check;

UPDATE patient_privacy_requests SET status = CASE
    WHEN status = 'pending'                        THEN 'requested'
    WHEN status = 'scheduled' AND kind = 'erasure' THEN 'review'
    WHEN status = 'scheduled'                      THEN 'requested'
    WHEN status = 'cancelled' AND kind = 'erasure' THEN 'rejected'
    WHEN status = 'cancelled'                      THEN 'failed'
    ELSE status END;

ALTER TABLE patient_privacy_requests ADD CONSTRAINT privacy_status_check
    CHECK (
        (kind = 'dsar'    AND status IN ('requested', 'executing',
                                         'completed', 'failed'))
     OR (kind = 'erasure' AND status IN ('requested', 'review', 'approved',
                                         'executing', 'completed', 'rejected'))
    );

-- ── Two-person rule: DB is the belt, the service is the braces ─────

ALTER TABLE patient_privacy_requests ADD CONSTRAINT privacy_two_person
    CHECK (kind <> 'erasure'
           OR reviewed_by IS NULL
           OR reviewed_by <> requested_by);

ALTER TABLE patient_privacy_requests ADD CONSTRAINT privacy_approved_has_review
    CHECK (kind <> 'erasure'
           OR status NOT IN ('approved', 'executing', 'completed')
           OR reviewed_by IS NOT NULL);

-- ── mdx_erasure grants + RLS policies ───────────────────────────────
-- The table list is the erasure fan-out (kept in lockstep with
-- erasure/fanout.py by step 05's CI gate): everything destroyable plus
-- the patients identity-overwrite. Deliberately NOT granted: DELETE on
-- patients (the tombstone row survives, status='erased'),
-- signed_envelopes (legal retention), audit.* (append-only forever).

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mdx_erasure') THEN
        RAISE EXCEPTION
            'mdx_erasure role missing — bootstrap via infra/postgres/init.sql '
            '(existing dev volumes: make reset-db)';
    END IF;
END $$;

GRANT SELECT, DELETE ON audio_files              TO mdx_erasure;
GRANT SELECT, DELETE ON transcription_jobs       TO mdx_erasure;
GRANT SELECT, DELETE ON dictation_sessions       TO mdx_erasure;
GRANT SELECT, DELETE ON reports                  TO mdx_erasure;
GRANT SELECT, DELETE ON report_versions          TO mdx_erasure;
GRANT SELECT, DELETE ON report_synthesis_jobs    TO mdx_erasure;
GRANT SELECT, DELETE ON encounters               TO mdx_erasure;
GRANT SELECT, DELETE ON clinical_notes           TO mdx_erasure;
GRANT SELECT, DELETE ON patient_consents         TO mdx_erasure;
GRANT SELECT, DELETE ON patient_anamnesis        TO mdx_erasure;
-- signing_sessions hold canonical_json (patient names) — transient
-- operational rows the engine hard-deletes.
GRANT SELECT, DELETE ON signing_sessions         TO mdx_erasure;
-- signed_envelopes: retained for RETAINED resources (legal evidence), but
-- an OUT-OF-RETENTION-WINDOW signed report is destroyed together with its
-- envelope — the grant enables that; the engine's classification decides.
GRANT SELECT, DELETE ON signed_envelopes         TO mdx_erasure;
GRANT SELECT, UPDATE ON patients                 TO mdx_erasure;  -- identity overwrite
GRANT SELECT, UPDATE ON patient_privacy_requests TO mdx_erasure;  -- engine transitions

-- FORCE RLS applies to mdx_erasure like any role — per-table policies,
-- tenant-predicated exactly like the app policies. No policy → no rows.
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'audio_files', 'transcription_jobs', 'dictation_sessions',
        'reports', 'report_synthesis_jobs', 'encounters', 'clinical_notes',
        'patient_consents', 'patient_anamnesis', 'signing_sessions',
        'signed_envelopes'
    ] LOOP
        EXECUTE format(
            'CREATE POLICY %I ON %I FOR SELECT TO mdx_erasure
             USING (tenant_id = current_setting(''app.tenant_id'', true)::uuid)',
            t || '_erasure_select', t);
        EXECUTE format(
            'CREATE POLICY %I ON %I FOR DELETE TO mdx_erasure
             USING (tenant_id = current_setting(''app.tenant_id'', true)::uuid)',
            t || '_erasure_delete', t);
    END LOOP;
END $$;

-- report_versions carries no tenant_id: scope via the parent report,
-- exactly like its app_role policies (the EXISTS subquery runs under
-- mdx_erasure's own reports SELECT policy).
CREATE POLICY report_versions_erasure_select ON report_versions
    FOR SELECT TO mdx_erasure
    USING (EXISTS (SELECT 1 FROM reports r
                   WHERE r.id = report_versions.report_id
                     AND r.tenant_id = current_setting('app.tenant_id', true)::uuid));
CREATE POLICY report_versions_erasure_delete ON report_versions
    FOR DELETE TO mdx_erasure
    USING (EXISTS (SELECT 1 FROM reports r
                   WHERE r.id = report_versions.report_id
                     AND r.tenant_id = current_setting('app.tenant_id', true)::uuid));

CREATE POLICY patients_erasure_select ON patients
    FOR SELECT TO mdx_erasure
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY patients_erasure_update ON patients
    FOR UPDATE TO mdx_erasure
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY patient_privacy_requests_erasure_select ON patient_privacy_requests
    FOR SELECT TO mdx_erasure
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY patient_privacy_requests_erasure_update ON patient_privacy_requests
    FOR UPDATE TO mdx_erasure
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
