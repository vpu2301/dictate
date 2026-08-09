-- Sprint 16 rollback — drop the scheduler tenant-enumeration function.
DROP FUNCTION IF EXISTS public.active_tenant_ids();
