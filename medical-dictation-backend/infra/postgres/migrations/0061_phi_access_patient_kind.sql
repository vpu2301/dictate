-- S15 — break-glass widens to patient records.
--
-- 0056 scoped `resource_kind` to 'report' on purpose: each new kind
-- needs its own enforcement point in a service before the door exists.
-- That enforcement point now exists — core-service gates the single
-- patient read (`GET /patients/{id}`, `/timeline`, `PUT`) the same way
-- report-service gates a report: `patient.read_full` for clinical
-- roles, a live grant for an administrator.
--
-- The roster LIST stays visible to tenant_admin in a redacted form
-- (name + id — enough to find the record to request), so the grant is
-- per-patient, like the per-report grant it mirrors.
--
-- For `resource_kind = 'patient'` the denormalised `patient_id` column
-- equals `resource_id`; kept redundantly so the oversight view's
-- group-by-patient works identically across both kinds.

ALTER TABLE phi_access_requests
    DROP CONSTRAINT phi_access_requests_resource_kind_check;

ALTER TABLE phi_access_requests
    ADD CONSTRAINT phi_access_requests_resource_kind_check
        CHECK (resource_kind IN ('report', 'patient'));
