DROP TABLE IF EXISTS signed_envelopes;
-- Cluster-global role: droppable only when no other database in the
-- cluster still has grants for it (mirrors the guarded CREATE in up).
DO $$
BEGIN
    DROP ROLE IF EXISTS app_public_verify;
EXCEPTION WHEN dependent_objects_still_exist THEN
    NULL;
END $$;
DROP TYPE  IF EXISTS signing_provider;
