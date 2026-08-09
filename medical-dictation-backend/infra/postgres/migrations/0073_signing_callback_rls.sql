-- Sprint 16 deployment — two signing_sessions RLS defects found by the
-- k3d staging smoke (and confirmed latent in compose):
--
-- 1. Migration 0020 granted app_callback_writer SELECT/UPDATE and left a
--    comment promising a "SECURITY DEFINER function in the service
--    layer" for the tenant-less callback lookup — the function was never
--    built, and the service does a plain SELECT. Under RLS FORCE with NO
--    policy for the role, every provider callback sees zero rows and
--    404s: signing via redirect providers (Дія / mock) cannot complete.
--    Fix: explicit permissive policies for app_callback_writer, USING
--    (true) — the ROLE is the boundary (it is held only by the callback
--    endpoint, whose every query is keyed by the unguessable
--    provider_session_id; same role-boundary reasoning as
--    crypto_writer's cross-tenant tenant_keks surface, libs/crypto).
--
-- 2. The `signing_sessions_tenant_update` policy exists in the long-
--    lived dev database but in NO migration — a hand-applied fix that
--    was never backported, so any fresh database (this cluster) refuses
--    app_role session transitions. Backported here, tenant-scoped and
--    guarded for the case where the hand-created policy is present.

DO $$
BEGIN
    CREATE POLICY signing_sessions_tenant_update ON signing_sessions
        FOR UPDATE TO app_role
        USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
EXCEPTION WHEN duplicate_object THEN
    NULL;
END $$;

CREATE POLICY signing_sessions_callback_select ON signing_sessions
    FOR SELECT TO app_callback_writer
    USING (true);
CREATE POLICY signing_sessions_callback_update ON signing_sessions
    FOR UPDATE TO app_callback_writer
    USING (true)
    WITH CHECK (true);
