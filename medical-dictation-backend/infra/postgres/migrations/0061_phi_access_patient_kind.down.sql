-- Reverse of 0061.
--
-- Patient-kind grants would violate the restored CHECK. Deleting them
-- destroys the record of every patient-level break-glass act — like the
-- 0056 down, acceptable ONLY as a dev-stack rollback; export the rows
-- first anywhere that has served real traffic. (The audit chain keeps
-- the `phi_access.granted` events, but the reason notes live only here.)
DELETE FROM phi_access_requests WHERE resource_kind = 'patient';

ALTER TABLE phi_access_requests
    DROP CONSTRAINT phi_access_requests_resource_kind_check;

ALTER TABLE phi_access_requests
    ADD CONSTRAINT phi_access_requests_resource_kind_check
        CHECK (resource_kind IN ('report'));
