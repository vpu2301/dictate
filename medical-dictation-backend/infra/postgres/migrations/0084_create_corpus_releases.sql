-- 0084: corpus_releases — immutable release register (sprint 21, ADR-0043 §5).
--
-- A release is a version string plus a SHA-256 manifest over the exact set
-- of accepted rows it ships (eval/corpus/v1/manifest.json pattern). Coverage
-- and WER regressions bisect to a release, not a commit. Rows are immutable
-- once published (trigger, audit.events pattern).

BEGIN;

CREATE TABLE corpus_releases (
    version         text        PRIMARY KEY
        CONSTRAINT release_semver_chk CHECK (version ~ '^v[0-9]+\.[0-9]+\.[0-9]+$'),
    created_at      timestamptz NOT NULL DEFAULT now(),
    manifest_sha256 text        NOT NULL
        CONSTRAINT release_sha_chk CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    phrase_count    integer     NOT NULL CHECK (phrase_count >= 0),
    published_by    uuid,       -- users.sub (soft reference)
    notes           text        NOT NULL DEFAULT ''
);

CREATE OR REPLACE FUNCTION corpus_releases_immutable() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'corpus_releases is immutable (operation=%, version=%)',
        TG_OP, COALESCE(OLD.version, NEW.version)
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER releases_no_update
    BEFORE UPDATE ON corpus_releases
    FOR EACH ROW EXECUTE FUNCTION corpus_releases_immutable();

CREATE TRIGGER releases_no_delete
    BEFORE DELETE ON corpus_releases
    FOR EACH ROW EXECUTE FUNCTION corpus_releases_immutable();

GRANT SELECT, INSERT ON corpus_releases TO app_role;
GRANT SELECT, INSERT ON corpus_releases TO tenant_writer;

ALTER TABLE corpus_releases ENABLE ROW LEVEL SECURITY;
ALTER TABLE corpus_releases FORCE ROW LEVEL SECURITY;

-- Releases are global artifacts (like medical_prompts, but with RLS kept on
-- for the check-rls gate): readable everywhere, publishable by the operator
-- CLI (tenant_writer) or a service context, never mutable.
CREATE POLICY releases_select ON corpus_releases
    FOR SELECT TO app_role, tenant_writer USING (true);

CREATE POLICY releases_insert ON corpus_releases
    FOR INSERT TO app_role, tenant_writer WITH CHECK (true);

CREATE POLICY releases_no_update_policy ON corpus_releases
    FOR UPDATE TO app_role, tenant_writer USING (false);

CREATE POLICY releases_no_delete_policy ON corpus_releases
    FOR DELETE TO app_role, tenant_writer USING (false);

COMMIT;
