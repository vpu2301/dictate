-- Sprint 12 — tenant enumeration for the background workers.
--
-- THE PROBLEM (found by running the workers against a live DB, not by
-- any unit test): the delivery worker and the digest job both need to
-- answer "which tenants have work?" BEFORE they can open a
-- tenant-scoped connection. That question is inherently cross-tenant,
-- so it cannot be asked from inside `tenant_connection`.
--
-- Asking it on a plain app_role connection fails in two different ways,
-- and the quiet one is worse:
--
--   * `SELECT DISTINCT tenant_id FROM notification_outbox` → the RLS
--     predicate evaluates `current_setting('app.tenant_id', true)::uuid`
--     with the setting unset, i.e. casts '' to uuid, and raises
--     InvalidTextRepresentationError. The delivery worker crashed on
--     every poll cycle.
--
--   * `SELECT id FROM tenants WHERE active` → RLS simply filters every
--     row out and returns EMPTY. The digest job "succeeded" while doing
--     nothing at all, which no error log would ever have revealed.
--
-- The fix follows the precedent set by migrations 0036/0037, which added
-- SECURITY DEFINER functions for exactly this shape of problem after RLS
-- silently no-oped the autocomplete counter updates (Sprint 10
-- verification). A SECURITY DEFINER function runs as its owner, so it
-- sees across tenants — and it is the ONLY sanctioned way to do so.
--
-- Both functions return nothing but tenant IDs. No row content crosses a
-- tenant boundary; the caller still opens `tenant_connection` per tenant
-- to do the actual work, so every read and write stays RLS-scoped.

-- Tenants with at least one outbox row due for delivery.
CREATE OR REPLACE FUNCTION notification_tenants_with_due_outbox()
RETURNS TABLE (tenant_id UUID)
LANGUAGE sql
SECURITY DEFINER
-- Pinned search_path: a SECURITY DEFINER function without one is a
-- privilege-escalation vector (a caller-controlled search_path could
-- shadow `notification_outbox` with their own table).
SET search_path = public, pg_temp
AS $$
    SELECT DISTINCT o.tenant_id
      FROM notification_outbox o
     WHERE o.status = 'pending'
       AND o.next_attempt_at <= now();
$$;

-- Active tenants, for the digest sweep.
CREATE OR REPLACE FUNCTION notification_active_tenant_ids()
RETURNS TABLE (tenant_id UUID)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT t.id FROM tenants t WHERE t.is_active = true;
$$;

REVOKE ALL ON FUNCTION notification_tenants_with_due_outbox() FROM PUBLIC;
REVOKE ALL ON FUNCTION notification_active_tenant_ids() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION notification_tenants_with_due_outbox() TO app_role;
GRANT EXECUTE ON FUNCTION notification_active_tenant_ids() TO app_role;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_notification_worker') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION notification_tenants_with_due_outbox() '
                'TO app_notification_worker';
        EXECUTE 'GRANT EXECUTE ON FUNCTION notification_active_tenant_ids() '
                'TO app_notification_worker';
    END IF;
END
$$;
