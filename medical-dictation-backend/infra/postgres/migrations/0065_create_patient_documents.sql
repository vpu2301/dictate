-- 0065 — patient_documents: files attached to a patient's record.
--
-- A referral letter, a lab PDF, a scan the patient brought on paper. The
-- BYTES never live here: the file is envelope-encrypted into MinIO
-- (mdx-patient-docs) via libs/storage.EncryptedObjectStore, and this table
-- holds the metadata plus the object URI. Same shape as audio_files, and for
-- the same reason — a PHI blob in a Postgres column is a blob no crypto-shred
-- can reach.
--
-- Erasure: registered in core_service.erasure.fanout as CRYPTO_SHRED (the
-- object dies first, then the row), so a GDPR Art. 17 request takes the
-- attachments with it. The FK to patients is ON DELETE CASCADE for the same
-- reason the rest of the record is.

CREATE TABLE patient_documents (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    patient_id      UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,

    -- What the clinician sees. `filename` is the name as uploaded (already
    -- sanitized by the service); `category` groups the list.
    filename        TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'other'
                        CHECK (category IN ('referral', 'lab', 'imaging',
                                            'discharge', 'consent', 'other')),
    note            TEXT NOT NULL DEFAULT '',

    -- Transport metadata, needed to serve the download back correctly.
    content_type    TEXT NOT NULL DEFAULT 'application/octet-stream',
    byte_size       BIGINT NOT NULL CHECK (byte_size >= 0),
    -- Integrity of the PLAINTEXT, computed before encryption. Lets a later
    -- read prove the file came back the way it went in.
    sha256          TEXT NOT NULL DEFAULT '',

    -- minio://mdx-patient-docs/<tenant>/<id>.enc — the ciphertext object.
    storage_uri     TEXT NOT NULL,
    -- Envelope header copy (key id, algorithm). Not the key.
    envelope_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    uploaded_by     UUID NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The list query: newest first, per patient.
CREATE INDEX patient_documents_patient_idx
    ON patient_documents (patient_id, created_at DESC, id);
CREATE INDEX patient_documents_tenant_idx
    ON patient_documents (tenant_id);

ALTER TABLE patient_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE patient_documents FORCE  ROW LEVEL SECURITY;

CREATE POLICY patient_documents_tenant_select ON patient_documents
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY patient_documents_tenant_insert ON patient_documents
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
-- Metadata is corrected by re-uploading, not edited in place: an attachment
-- whose description can change under a reader is not evidence.
CREATE POLICY patient_documents_tenant_update ON patient_documents
    FOR UPDATE TO app_role
    USING (false);
-- Deleting an attachment is a real operation (wrong file, wrong patient), and
-- the service crypto-shreds the object before the row.
CREATE POLICY patient_documents_tenant_delete ON patient_documents
    FOR DELETE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY patient_documents_tenant_restrictive ON patient_documents
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT, DELETE ON patient_documents TO app_role;

-- ── Erasure role (0044) ─────────────────────────────────────────────
-- A new patient-linked PHI table must be reachable by the erasure engine on
-- the day it is created, not on the day someone notices. Same grant + policy
-- pair every other fan-out table has.
GRANT SELECT, DELETE ON patient_documents TO mdx_erasure;

CREATE POLICY patient_documents_erasure_select ON patient_documents
    FOR SELECT TO mdx_erasure
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY patient_documents_erasure_delete ON patient_documents
    FOR DELETE TO mdx_erasure
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
