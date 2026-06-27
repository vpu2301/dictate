-- M1 / B4 — persist the expected document hash on the signing session.
--
-- The local-KEP upload flow (POST /signing/sessions/{id}/upload) must bind
-- the signed PDF to the exact document the session was opened for. The FE
-- computes sha256(rendered_pdf) and passes it at initiate as
-- ``document_pdf_hash_hex``; we now store it so the upload handler can
-- reject (422) a signature over any other bytes. Nullable so pre-existing
-- rows and remote-provider (Дія) sessions — which bind via the provider
-- callback instead — are unaffected.

ALTER TABLE signing_sessions
    ADD COLUMN document_pdf_hash BYTEA;
