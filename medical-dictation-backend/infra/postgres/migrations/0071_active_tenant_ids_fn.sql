-- Sprint 16 — cross-tenant enumeration for in-process scheduled jobs.
--
-- The idle-draft cleanup (report-service) and the erasure backup-horizon
-- notifier (core-service) sweep EVERY tenant, but `tenants` is RLS-FORCEd
-- to self-select for app_role, so an in-process job under app_role sees
-- nothing (the sprint-08 chain reconciler carries exactly this latent
-- no_tenants_visible warning). Same remedy as tenant_of_sub (0027) and
-- the sprint-10 SECURITY DEFINER counters (0036/0037): a narrow
-- definer-owned function that leaks only what the job needs — the ids of
-- active tenants, nothing else.
--
-- The reserved global tenant (0068) is excluded: it holds no clinician
-- drafts and no patients; sweeping it would only create noise.

CREATE FUNCTION public.active_tenant_ids()
    RETURNS SETOF uuid
    LANGUAGE sql
    STABLE
    SECURITY DEFINER
    SET search_path = public
AS $$
    SELECT id FROM public.tenants
    WHERE is_active = true
      AND id <> '00000000-0000-0000-0000-000000000000'::uuid
    ORDER BY id
$$;

REVOKE ALL ON FUNCTION public.active_tenant_ids() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.active_tenant_ids() TO app_role;
