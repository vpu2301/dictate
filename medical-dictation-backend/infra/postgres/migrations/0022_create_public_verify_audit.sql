-- Sprint 09 / Day 2 — public_verify_audit.
--
-- Global, no-tenant audit stream for the public ``GET /verify/{token}``
-- endpoint. NOT in the sprint-02 hash-chained ``audit.events`` log
-- because public-verify calls have no tenant context.
--
-- The IP is hashed with a system-wide HMAC key (rotated yearly; old
-- HMACs not back-rotated — privacy over linkability across years).

CREATE TABLE audit.public_verify_audit (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_kind          TEXT NOT NULL,
    verification_token  TEXT NOT NULL,
    requestor_ip_hmac   BYTEA NOT NULL,
    user_agent_hash     BYTEA,
    result              TEXT NOT NULL CHECK (result IN ('valid', 'invalid', 'not_found', 'rate_limited')),
    bytes_returned      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX public_verify_audit_token_idx
    ON audit.public_verify_audit (verification_token, created_at DESC);
CREATE INDEX public_verify_audit_result_idx
    ON audit.public_verify_audit (result, created_at DESC);
CREATE INDEX public_verify_audit_ip_idx
    ON audit.public_verify_audit (requestor_ip_hmac, created_at DESC);

GRANT INSERT, SELECT ON audit.public_verify_audit TO audit_writer;
GRANT SELECT ON audit.public_verify_audit TO audit_reader;
