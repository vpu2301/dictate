-- Sprint 09 / Day 2 — signed_envelopes.
--
-- One row per successfully-signed (or signed-amendment) report
-- version. Sprint-08 keeps the version-row pointer; this table is the
-- canonical home of the cryptographic artifact and is referenced from
-- ``report_versions.signing_record_id``.
--
-- Rows are append-only (DELETE forbidden). UPDATEs limited to LTV
-- backfill columns (``ltv_enabled``, ``ocsp_responses``,
-- ``tsa_response``) — sprint-17 will tighten this further.

CREATE TYPE signing_provider AS ENUM ('diia', 'iit', 'mock');

CREATE TABLE signed_envelopes (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,

    -- Authorial linkage.
    signer_user_id          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,

    -- Resource binding.
    resource_type           TEXT NOT NULL CHECK (resource_type IN ('report', 'amendment', 'note', 'anamnesis', 'consent')),
    resource_id             UUID NOT NULL,
    resource_version_id     UUID NOT NULL,

    -- Provider provenance.
    provider                signing_provider NOT NULL,
    provider_session_id     TEXT NOT NULL,
    provider_envelope_id    TEXT NOT NULL,

    -- Canonical payload + hash (sprint-09 canonicalize_report output).
    canonical_json          JSONB NOT NULL,
    canonical_json_hash     BYTEA NOT NULL,

    -- Signed bytes + algorithm.
    signed_at               TIMESTAMPTZ NOT NULL,
    signed_data             BYTEA NOT NULL,
    signature_algorithm     TEXT NOT NULL,

    -- Public verify token (16 random bytes, base64url; unique).
    verification_token      TEXT NOT NULL,

    -- Storage uri of the signed PDF (MinIO / S3 in prod; tmpfs in demo).
    pdf_storage_uri         TEXT,

    -- Signer identification.
    signer_ipn_hmac         BYTEA,           -- HMAC of the IPN with system key
    signer_full_name        TEXT NOT NULL,

    -- Certificate provenance (legal evidence; carry for 25-yr retention).
    certificate_serial      TEXT NOT NULL,
    certificate_issuer_cn   TEXT NOT NULL,
    certificate_chain       TEXT[] NOT NULL DEFAULT '{}',

    -- TSA + OCSP evidence (LTV).
    tsa_response            BYTEA,
    ocsp_responses          BYTEA[] NOT NULL DEFAULT '{}',
    is_qualified            BOOLEAN NOT NULL DEFAULT FALSE,
    ltv_enabled             BOOLEAN NOT NULL DEFAULT FALSE,

    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT signed_envelopes_token_unique UNIQUE (verification_token),
    CONSTRAINT signed_envelopes_per_resource_version_unique
        UNIQUE (tenant_id, resource_type, resource_version_id)
);

CREATE INDEX signed_envelopes_tenant_resource_idx
    ON signed_envelopes (tenant_id, resource_type, resource_id, signed_at DESC);
CREATE INDEX signed_envelopes_signer_idx
    ON signed_envelopes (tenant_id, signer_user_id, signed_at DESC);
CREATE INDEX signed_envelopes_signed_at_idx
    ON signed_envelopes (signed_at DESC);

-- ── RLS ─────────────────────────────────────────────────────────────

ALTER TABLE signed_envelopes ENABLE ROW LEVEL SECURITY;
ALTER TABLE signed_envelopes FORCE  ROW LEVEL SECURITY;

CREATE POLICY signed_envelopes_tenant_select ON signed_envelopes
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY signed_envelopes_tenant_insert ON signed_envelopes
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY signed_envelopes_tenant_update ON signed_envelopes
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY signed_envelopes_tenant_delete ON signed_envelopes
    FOR DELETE TO app_role
    USING (false);

CREATE POLICY signed_envelopes_tenant_restrictive ON signed_envelopes
    AS RESTRICTIVE
    FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE ON signed_envelopes TO app_role;

-- ── Public verify lookup role ───────────────────────────────────────
-- The public ``GET /verify/{token}`` endpoint runs as a separate
-- low-privilege role that bypasses RLS via SECURITY DEFINER, allowing
-- token-based lookups without an authenticated tenant context.
CREATE ROLE app_public_verify;

GRANT SELECT (
    id, resource_type, signed_at, verification_token,
    pdf_storage_uri, signer_full_name,
    certificate_serial, certificate_issuer_cn,
    is_qualified, signature_algorithm,
    canonical_json_hash, signed_data
) ON signed_envelopes TO app_public_verify;

CREATE POLICY signed_envelopes_public_token ON signed_envelopes
    FOR SELECT TO app_public_verify
    USING (verification_token IS NOT NULL);
