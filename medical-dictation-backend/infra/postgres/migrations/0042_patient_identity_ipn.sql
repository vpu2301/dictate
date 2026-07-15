-- S11 step 01 — patient identity hardening: ІПН (РНОКПП) + `erased` status.
--
-- Ukrainian clinical reality identifies a patient by the 10-digit ІПН
-- (РНОКПП), not by name + MRN. Storing it raw would make the roster a
-- national-ID lookup table, so the row carries:
--
--   ipn_hmac      — HMAC-SHA256(MDX_PATIENT_IPN_HMAC_KEY, normalized ІПН).
--                   The ONLY value ever used in queries (exact lookup,
--                   duplicate prevention). Deliberately keyed separately
--                   from signing-service's SIGNER_IPN_HMAC_KEY (ADR-0027).
--   ipn_encrypted — envelope ciphertext (iv‖tag‖ct) of the raw ІПН;
--                   populated only when PATIENT_IPN_RAW_ENABLED=true
--                   (DPO-gated, default off — see todo.md).
--   ipn_dek       — the wrapped per-row DEK (dek_iv‖dek_tag‖wrapped_dek)
--                   for ipn_encrypted. NULL together with it; nulling this
--                   column crypto-shreds the raw ІПН (ADR-0027).
--
-- `erased` becomes the terminal patient status for the right-to-erasure
-- engine (step 07). It is set only by that engine's dedicated path — the
-- service PUT surface rejects it — and always carries `erased_at`.
--
-- The unique index excludes erased rows: erasure nulls ipn_hmac
-- (ADR-0027 records that branch), and a returning patient must be
-- re-registrable afterwards either way.

ALTER TABLE patients
    ADD COLUMN ipn_hmac      BYTEA,
    ADD COLUMN ipn_encrypted BYTEA,
    ADD COLUMN ipn_dek       BYTEA,
    ADD COLUMN erased_at     TIMESTAMPTZ;

ALTER TABLE patients DROP CONSTRAINT patients_status_check;
ALTER TABLE patients ADD CONSTRAINT patients_status_check
    CHECK (status IN ('active', 'inactive', 'deceased', 'erased'));

ALTER TABLE patients ADD CONSTRAINT patients_erased_has_ts
    CHECK ((status = 'erased') = (erased_at IS NOT NULL));

ALTER TABLE patients ADD CONSTRAINT patients_ipn_enc_pair
    CHECK ((ipn_encrypted IS NULL) = (ipn_dek IS NULL));

-- One live patient per ІПН per tenant. Partial: erased rows drop out so
-- re-registration after erasure never collides with the tombstone.
CREATE UNIQUE INDEX uq_patients_tenant_ipn
    ON patients (tenant_id, ipn_hmac)
    WHERE ipn_hmac IS NOT NULL AND status <> 'erased';
