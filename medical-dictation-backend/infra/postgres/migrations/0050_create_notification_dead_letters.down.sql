-- Reverse of 0050_create_notification_dead_letters.sql.
--
-- Dropping the table takes its own grants with it, but the schema-level
-- USAGE granted to app_notification_worker survives and would block the
-- DROP ROLE in 0046.down. Each migration revokes exactly what it
-- granted, so the teardown order matches the setup order.

DROP TABLE IF EXISTS audit.notification_dead_letters;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_notification_worker') THEN
        REVOKE USAGE ON SCHEMA audit FROM app_notification_worker;
    END IF;
END
$$;

-- app_role's audit-schema USAGE was granted by this migration, so it is
-- withdrawn here. Nothing else app_role does touches the audit schema.
REVOKE USAGE ON SCHEMA audit FROM app_role;
