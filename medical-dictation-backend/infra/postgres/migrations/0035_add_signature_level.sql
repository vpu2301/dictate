-- S09-revision — signature_level: the immutable legal tier of an envelope.
--
-- Two invariants this migration enforces at the database layer:
--
-- 1. ``signature_level`` on an envelope is **write-once** (trigger): no
--    UPDATE may ever change it, nor the provider, canonical hash,
--    canonical JSON, envelope bytes, verification token, or signing
--    time. LTV backfill columns (``ltv_enabled``, ``ocsp_responses``,
--    ``tsa_response``) and ``pdf_storage_uri`` stay mutable on purpose.
--    ``is_qualified`` may only ever be *downgraded* (revocation
--    re-checks), never upgraded.
--
-- 2. Cross-CHECKs bind provider to tier: ``dev_password`` can only
--    produce ``dev`` envelopes; ``file_key``/``diia``/``iit`` can only
--    produce ``qualified``-tier envelopes (whether they *verify* as
--    qualified is decided by the trust store at verify time, never by
--    the tier column alone). A ``dev`` envelope can never carry
--    ``is_qualified = true``.
--
-- ``signature_level`` semantics: 'qualified' means the envelope carries
-- a real CMS/PAdES cryptographic envelope that *purports* to be a
-- qualified signature; 'dev' means a development-scaffold confirmation
-- with no cryptographic envelope at all. The mock provider emits real
-- (test-CA-anchored) CMS envelopes so CI exercises the full qualified
-- verification path — the trust store guarantees those never *report*
-- as qualified.

-- ── signed_envelopes.signature_level ────────────────────────────────

ALTER TABLE signed_envelopes ADD COLUMN signature_level TEXT;

-- Backfill: every pre-existing row came from diia/iit/mock — all of
-- which persist real CMS envelopes (qualified tier).
UPDATE signed_envelopes SET signature_level = 'qualified';

ALTER TABLE signed_envelopes ALTER COLUMN signature_level SET NOT NULL;

ALTER TABLE signed_envelopes
    ADD CONSTRAINT signed_envelopes_signature_level_check
    CHECK (signature_level IN ('qualified', 'dev'));

ALTER TABLE signed_envelopes
    ADD CONSTRAINT signed_envelopes_level_provider_check
    CHECK (
        (provider = 'dev_password' AND signature_level = 'dev')
        OR (provider IN ('file_key', 'diia', 'iit') AND signature_level = 'qualified')
        OR (provider = 'mock')
    );

-- A dev envelope can never be flagged qualified, no matter what.
ALTER TABLE signed_envelopes
    ADD CONSTRAINT signed_envelopes_dev_never_qualified_check
    CHECK (signature_level = 'qualified' OR is_qualified = false);

-- ── Write-once immutability trigger ─────────────────────────────────

CREATE FUNCTION signed_envelopes_immutability_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.signature_level IS DISTINCT FROM OLD.signature_level THEN
        RAISE EXCEPTION 'signed_envelopes.signature_level is immutable';
    END IF;
    IF NEW.provider IS DISTINCT FROM OLD.provider THEN
        RAISE EXCEPTION 'signed_envelopes.provider is immutable';
    END IF;
    IF NEW.canonical_json_hash IS DISTINCT FROM OLD.canonical_json_hash THEN
        RAISE EXCEPTION 'signed_envelopes.canonical_json_hash is immutable';
    END IF;
    IF NEW.canonical_json IS DISTINCT FROM OLD.canonical_json THEN
        RAISE EXCEPTION 'signed_envelopes.canonical_json is immutable';
    END IF;
    IF NEW.signed_data IS DISTINCT FROM OLD.signed_data THEN
        RAISE EXCEPTION 'signed_envelopes.signed_data is immutable';
    END IF;
    IF NEW.verification_token IS DISTINCT FROM OLD.verification_token THEN
        RAISE EXCEPTION 'signed_envelopes.verification_token is immutable';
    END IF;
    IF NEW.signed_at IS DISTINCT FROM OLD.signed_at THEN
        RAISE EXCEPTION 'signed_envelopes.signed_at is immutable';
    END IF;
    IF NEW.is_qualified AND NOT OLD.is_qualified THEN
        RAISE EXCEPTION 'signed_envelopes.is_qualified may never be upgraded';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER signed_envelopes_immutability
    BEFORE UPDATE ON signed_envelopes
    FOR EACH ROW
    EXECUTE FUNCTION signed_envelopes_immutability_guard();

-- ── Public verify may read the tier ─────────────────────────────────

GRANT SELECT (signature_level, provider) ON signed_envelopes TO app_public_verify;

-- ── signing_sessions.canonical_json ─────────────────────────────────
-- The canonical JCS object the flow committed to at initiate time.
-- Fixes the sprint-09 hand-off gap where the callback path persisted
-- ``{}``: the report-service sign surface now supplies the real
-- canonical object up front and every persist path copies it into the
-- envelope row.

ALTER TABLE signing_sessions ADD COLUMN canonical_json JSONB;
