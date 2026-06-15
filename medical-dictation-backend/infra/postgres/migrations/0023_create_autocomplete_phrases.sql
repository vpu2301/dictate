-- Sprint 10 / Day 1 — autocomplete_phrases.
--
-- Three concentric scopes (ADR-0025):
--   system  → tenant_id NULL, owner_user_id NULL  (visible to everyone)
--   tenant  → tenant_id set,   owner_user_id NULL  (visible to all in tenant)
--   user    → tenant_id set,   owner_user_id set   (visible to one user)
--
-- RLS PERMISSIVE policy ``tenant_visibility`` enforces the visibility
-- model. A separate RESTRICTIVE policy ``write_user_phrases`` enforces
-- write rules — only the owning user can write user-source rows; only
-- admins can write tenant-source rows; system rows require the
-- ``tenant_writer`` service role.

CREATE TYPE autocomplete_source AS ENUM ('system', 'tenant', 'user');

CREATE TABLE autocomplete_phrases (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID REFERENCES tenants(id) ON DELETE CASCADE,
    owner_user_id   UUID REFERENCES users(sub) ON DELETE CASCADE,
    phrase          TEXT NOT NULL,
    language        TEXT NOT NULL CHECK (language IN ('uk', 'en')),
    specialty       TEXT,
    section_hint    TEXT,
    source          autocomplete_source NOT NULL,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,

    -- Ranking counters (eventually-consistent — updated nightly by
    -- the roll-up job from the telemetry partitions).
    impression_count BIGINT NOT NULL DEFAULT 0,
    acceptance_count BIGINT NOT NULL DEFAULT 0,
    last_accepted_at TIMESTAMPTZ,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT phrase_max_80_chars CHECK (char_length(phrase) BETWEEN 1 AND 80),
    CONSTRAINT user_phrases_have_owner CHECK (
        (source = 'user'  AND owner_user_id IS NOT NULL AND tenant_id IS NOT NULL)
        OR (source = 'tenant' AND owner_user_id IS NULL AND tenant_id IS NOT NULL)
        OR (source = 'system' AND owner_user_id IS NULL AND tenant_id IS NULL)
    ),
    CONSTRAINT phrase_acceptance_lte_impression CHECK (acceptance_count <= impression_count)
);

CREATE INDEX autocomplete_phrases_corpus_idx
    ON autocomplete_phrases (tenant_id, language, source, enabled);
CREATE INDEX autocomplete_phrases_user_scope_idx
    ON autocomplete_phrases (tenant_id, owner_user_id)
    WHERE owner_user_id IS NOT NULL;
CREATE INDEX autocomplete_phrases_unique_phrase_per_owner
    ON autocomplete_phrases (
        coalesce(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid),
        coalesce(owner_user_id, '00000000-0000-0000-0000-000000000000'::uuid),
        phrase, language
    );
-- Admin search UI (trigram), not on the hot path.
CREATE INDEX autocomplete_phrases_trgm_idx
    ON autocomplete_phrases USING gin (phrase gin_trgm_ops);

-- ── RLS ─────────────────────────────────────────────────────────────

ALTER TABLE autocomplete_phrases ENABLE ROW LEVEL SECURITY;
ALTER TABLE autocomplete_phrases FORCE  ROW LEVEL SECURITY;

-- PERMISSIVE: visibility model (system + own tenant + own user-rows).
CREATE POLICY tenant_visibility ON autocomplete_phrases
    FOR SELECT TO app_role
    USING (
        source = 'system'
        OR tenant_id = current_setting('app.tenant_id', true)::uuid
    );

-- RESTRICTIVE: write rules.
-- INSERT: caller may insert
--   user-source rows only as the owning user;
--   tenant-source rows only as an admin / tenant_admin;
--   system-source rows are forbidden to app_role (use tenant_writer).
CREATE POLICY write_user_phrases ON autocomplete_phrases
    AS RESTRICTIVE
    FOR INSERT TO app_role
    WITH CHECK (
        (
            source = 'user'
            AND tenant_id = current_setting('app.tenant_id', true)::uuid
            AND owner_user_id = current_setting('app.user_id', true)::uuid
        )
        OR (
            source = 'tenant'
            AND tenant_id = current_setting('app.tenant_id', true)::uuid
            AND owner_user_id IS NULL
            AND current_setting('app.user_role', true) IN ('admin', 'tenant_admin')
        )
    );

-- UPDATE: same RESTRICTIVE shape — you can mutate own row, admins can
-- mutate tenant rows. Soft-deletes use UPDATE (enabled=false).
CREATE POLICY update_user_phrases ON autocomplete_phrases
    AS RESTRICTIVE
    FOR UPDATE TO app_role
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

-- DELETE: forbidden via app_role. Soft-delete with UPDATE enabled=false.
CREATE POLICY delete_forbidden ON autocomplete_phrases
    FOR DELETE TO app_role
    USING (false);

GRANT SELECT, INSERT, UPDATE ON autocomplete_phrases TO app_role;

-- Tenant_writer service role: used by migration 0026 corpus seed.
-- NOTE (Sprint A1): `tenant_writer` is a global role bootstrapped in
-- infra/postgres/init.sql and already GRANT-ed to from migration 0002, so an
-- unguarded `CREATE ROLE tenant_writer` here aborts a clean migrate-up with
-- "role already exists". Removed; the role is guaranteed to pre-exist.
GRANT SELECT, INSERT, UPDATE ON autocomplete_phrases TO tenant_writer;
-- The seed role bypasses RLS for system-source rows by exclusion: its
-- policy is permissive of all source=system inserts.
CREATE POLICY seed_system_rows ON autocomplete_phrases
    FOR INSERT TO tenant_writer
    WITH CHECK (source = 'system');
CREATE POLICY seed_system_select ON autocomplete_phrases
    FOR SELECT TO tenant_writer USING (true);
CREATE POLICY seed_system_update ON autocomplete_phrases
    FOR UPDATE TO tenant_writer USING (source = 'system') WITH CHECK (source = 'system');
