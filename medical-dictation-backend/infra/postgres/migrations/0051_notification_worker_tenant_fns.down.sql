-- Reverse of 0051_notification_worker_tenant_fns.sql.

DROP FUNCTION IF EXISTS notification_tenants_with_due_outbox();
DROP FUNCTION IF EXISTS notification_active_tenant_ids();
