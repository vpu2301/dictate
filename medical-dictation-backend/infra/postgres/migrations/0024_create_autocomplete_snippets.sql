-- Sprint 10 / Day 1 — autocomplete_snippets.
--
-- Snippets are short triggers (e.g. "/cv") that expand to longer
-- text. Same three-scope model as phrases. The UNIQUE constraint on
-- ``(tenant_id, owner_user_id, trigger)`` allows the same trigger
-- across users, with user-scoped winning at lookup time.

CREATE TABLE autocomplete_snippets (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID REFERENCES tenants(id) ON DELETE CASCADE,
    owner_user_id   UUID REFERENCES users(sub) ON DELETE CASCADE,
    trigger         TEXT NOT NULL,
    expansion       TEXT NOT NULL,
    cursor_position INTEGER NOT NULL DEFAULT 0,
    language        TEXT NOT NULL CHECK (language IN ('uk', 'en')),
    source          autocomplete_source NOT NULL,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT trigger_format CHECK (trigger ~ '^[a-z][a-z0-9_-]{0,30}$'),
    CONSTRAINT expansion_max CHECK (char_length(expansion) BETWEEN 1 AND 4000),
    CONSTRAINT user_snippets_have_owner CHECK (
        (source = 'user'   AND owner_user_id IS NOT NULL AND tenant_id IS NOT NULL)
        OR (source = 'tenant' AND owner_user_id IS NULL AND tenant_id IS NOT NULL)
        OR (source = 'system' AND owner_user_id IS NULL AND tenant_id IS NULL)
    )
);

CREATE UNIQUE INDEX autocomplete_snippets_unique_trigger_per_owner
    ON autocomplete_snippets (
        coalesce(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid),
        coalesce(owner_user_id, '00000000-0000-0000-0000-000000000000'::uuid),
        trigger, language
    );

ALTER TABLE autocomplete_snippets ENABLE ROW LEVEL SECURITY;
ALTER TABLE autocomplete_snippets FORCE  ROW LEVEL SECURITY;

CREATE POLICY tenant_visibility ON autocomplete_snippets
    FOR SELECT TO app_role
    USING (
        source = 'system'
        OR tenant_id = current_setting('app.tenant_id', true)::uuid
    );

CREATE POLICY write_user_snippets ON autocomplete_snippets
    AS RESTRICTIVE FOR INSERT TO app_role
    WITH CHECK (
        (source = 'user'
         AND tenant_id = current_setting('app.tenant_id', true)::uuid
         AND owner_user_id = current_setting('app.user_id', true)::uuid)
        OR (source = 'tenant'
            AND tenant_id = current_setting('app.tenant_id', true)::uuid
            AND owner_user_id IS NULL
            AND current_setting('app.user_role', true) IN ('admin', 'tenant_admin'))
    );

CREATE POLICY update_user_snippets ON autocomplete_snippets
    AS RESTRICTIVE FOR UPDATE TO app_role
    USING (
        (source = 'user'
         AND tenant_id = current_setting('app.tenant_id', true)::uuid
         AND owner_user_id = current_setting('app.user_id', true)::uuid)
        OR (source = 'tenant'
            AND tenant_id = current_setting('app.tenant_id', true)::uuid
            AND current_setting('app.user_role', true) IN ('admin', 'tenant_admin'))
    )
    WITH CHECK (
        (source = 'user'
         AND tenant_id = current_setting('app.tenant_id', true)::uuid
         AND owner_user_id = current_setting('app.user_id', true)::uuid)
        OR (source = 'tenant'
            AND tenant_id = current_setting('app.tenant_id', true)::uuid
            AND current_setting('app.user_role', true) IN ('admin', 'tenant_admin'))
    );

CREATE POLICY snippets_delete_forbidden ON autocomplete_snippets
    FOR DELETE TO app_role USING (false);

GRANT SELECT, INSERT, UPDATE ON autocomplete_snippets TO app_role;
GRANT SELECT, INSERT, UPDATE ON autocomplete_snippets TO tenant_writer;

CREATE POLICY seed_system_snippets_select ON autocomplete_snippets
    FOR SELECT TO tenant_writer USING (true);
CREATE POLICY seed_system_snippets_insert ON autocomplete_snippets
    FOR INSERT TO tenant_writer
    WITH CHECK (source = 'system');
CREATE POLICY seed_system_snippets_update ON autocomplete_snippets
    FOR UPDATE TO tenant_writer USING (source = 'system') WITH CHECK (source = 'system');
