-- Sprint 10 fix — app_role write access on phrases/snippets was deny-all.
--
-- Migrations 0023/0024 gave app_role only RESTRICTIVE write policies
-- (write_user_phrases / update_user_phrases and the snippet twins).
-- Postgres requires at least one PERMISSIVE policy per command for access;
-- restrictive-only means deny-all. Live effect: POST /autocomplete/phrases
-- always 500'd with an RLS violation and DELETE (soft-delete UPDATE)
-- silently matched 0 rows and 404'd — the write API never worked.
--
-- These PERMISSIVE policies draw the coarse tenant boundary; the existing
-- RESTRICTIVE policies still AND in the fine-grained rules (user rows only
-- by owner, tenant rows only by admins, system rows never for app_role —
-- system rows have tenant_id NULL so they fail the tenant match here too).

CREATE POLICY app_insert_phrases ON autocomplete_phrases
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY app_update_phrases ON autocomplete_phrases
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY app_insert_snippets ON autocomplete_snippets
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY app_update_snippets ON autocomplete_snippets
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
