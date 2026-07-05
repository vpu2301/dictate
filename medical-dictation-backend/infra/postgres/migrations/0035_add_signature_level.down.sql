-- Down: remove the signature_level surface added in 0035.

ALTER TABLE signing_sessions DROP COLUMN IF EXISTS canonical_json;

REVOKE SELECT (signature_level, provider) ON signed_envelopes FROM app_public_verify;

DROP TRIGGER IF EXISTS signed_envelopes_immutability ON signed_envelopes;
DROP FUNCTION IF EXISTS signed_envelopes_immutability_guard();

ALTER TABLE signed_envelopes DROP CONSTRAINT IF EXISTS signed_envelopes_dev_never_qualified_check;
ALTER TABLE signed_envelopes DROP CONSTRAINT IF EXISTS signed_envelopes_level_provider_check;
ALTER TABLE signed_envelopes DROP CONSTRAINT IF EXISTS signed_envelopes_signature_level_check;
ALTER TABLE signed_envelopes DROP COLUMN IF EXISTS signature_level;
