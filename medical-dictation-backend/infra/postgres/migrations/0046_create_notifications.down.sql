-- Reverse of 0046_create_notifications.sql.
--
-- The role is dropped last and only after its grants are gone. DROP ROLE
-- fails while any dependent privilege remains, so REVOKE first.

DROP TABLE IF EXISTS notifications;

-- Belt-and-braces: 0047-0050 each revoke their own grants, but a
-- partially-applied stack (or a hand-run migration) can leave one
-- behind, and a stray privilege makes DROP ROLE fail with a message
-- that names the dependency but not the fix.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_notification_worker') THEN
        EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM app_notification_worker';
        EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA audit FROM app_notification_worker';
        EXECUTE 'REVOKE ALL ON SCHEMA public FROM app_notification_worker';
        EXECUTE 'REVOKE ALL ON SCHEMA audit FROM app_notification_worker';
        DROP ROLE app_notification_worker;
    END IF;
END
$$;
