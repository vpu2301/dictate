-- Down for 0042 — remove ІПН identity columns and the `erased` status.
--
-- Any rows already in `erased` state are folded back to `inactive` so the
-- original three-value CHECK can be restored; the identity columns are
-- dropped wholesale (they carry no data the up-migration didn't add).

DROP INDEX IF EXISTS uq_patients_tenant_ipn;

ALTER TABLE patients DROP CONSTRAINT patients_ipn_enc_pair;
ALTER TABLE patients DROP CONSTRAINT patients_erased_has_ts;

UPDATE patients SET status = 'inactive' WHERE status = 'erased';

ALTER TABLE patients DROP CONSTRAINT patients_status_check;
ALTER TABLE patients ADD CONSTRAINT patients_status_check
    CHECK (status IN ('active', 'inactive', 'deceased'));

ALTER TABLE patients
    DROP COLUMN ipn_hmac,
    DROP COLUMN ipn_encrypted,
    DROP COLUMN ipn_dek,
    DROP COLUMN erased_at;
