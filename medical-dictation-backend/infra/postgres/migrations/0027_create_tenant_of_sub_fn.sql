-- Sprint A1 / DEF-A1-20 — resolve a user's tenant from their `sub` without an
-- incumbent tenant context.
--
-- The refresh-replay detection path (auth-service) has only the consumed
-- refresh token, which carries `sub` but NOT the custom `tid` claim (Keycloak
-- refresh tokens are minimal, and the tenant attribute is an unmanaged user
-- attribute hidden from the admin API in Keycloak 24). Without a tenant it
-- cannot write the `auth.refresh_replay_detected` audit event.
--
-- This SECURITY DEFINER function performs a narrow, single-column lookup that
-- bypasses RLS (it runs as the owner). It is intentionally minimal: one input
-- (sub), one output (tenant_id), no row data leaked. EXECUTE is granted only to
-- the application role.

CREATE FUNCTION public.tenant_of_sub(p_sub uuid)
    RETURNS uuid
    LANGUAGE sql
    STABLE
    SECURITY DEFINER
    SET search_path = public
AS $$
    SELECT tenant_id FROM public.users WHERE sub = p_sub
$$;

REVOKE ALL ON FUNCTION public.tenant_of_sub(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.tenant_of_sub(uuid) TO app_role;
