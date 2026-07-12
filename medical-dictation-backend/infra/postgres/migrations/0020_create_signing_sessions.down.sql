DROP TABLE IF EXISTS signing_sessions;
-- Cluster-global role: droppable only when no other database in the
-- cluster still has grants for it (mirrors the guarded CREATE in up).
DO $$
BEGIN
    DROP ROLE IF EXISTS app_callback_writer;
EXCEPTION WHEN dependent_objects_still_exist THEN
    NULL;
END $$;
DROP TYPE  IF EXISTS signing_session_status;
