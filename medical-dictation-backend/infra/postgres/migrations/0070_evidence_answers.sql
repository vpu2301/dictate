-- 0070_evidence_answers.sql
-- EVA-S04: the Quick-Search web agent's two tables.
--
-- The Q&A tables themselves (questions/answers/answer_segments/
-- answer_provenance/answer_traces/followups) shipped in 0067 and are only
-- *activated* by this sprint — no DDL change is needed for them.
--
-- New here:
--   web_domains — the fetch allowlist (FR-6). Resolution is tenant-first:
--     a tenant row for a domain overrides the shipped GLOBAL row, which is
--     how a tenant disables a default without mutating shared data. The
--     shipped set is seeded once under the reserved GLOBAL tenant (the S02
--     corpus pattern) instead of being copied into every tenant at creation
--     time — one reviewed table, no per-tenant drift.
--   web_pages — the per-tenant cache of fetched pages. A cached snapshot is
--     what a reopened answer renders from: reopen must never re-fetch
--     (AC-S04-B-6), so the row is the evidence that the page said what the
--     answer claims it said.
--
-- Both tables are RLS + FORCE with a RESTRICTIVE catch-all, per 0063/0065.
-- Trust tiers use the frozen WebTrustTier contract vocabulary (ADR-0002) so
-- the column and the wire model cannot drift apart.

-- ---------------------------------------------------------------- allowlist

CREATE TABLE web_domains (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid NOT NULL,
    domain       text NOT NULL,
    trust_tier   text NOT NULL CHECK (trust_tier IN
                   ('international_organization', 'government', 'professional_society',
                    'guideline_registry', 'journal', 'other')),
    status       text NOT NULL DEFAULT 'pending' CHECK (status IN
                   ('enabled', 'pending', 'disabled')),
    -- true for the shipped, clinically reviewed set (docs/corpus/web-domains.md).
    is_default   boolean NOT NULL DEFAULT false,
    -- Pages here are typically paywalled: cite metadata, never body text.
    metadata_only boolean NOT NULL DEFAULT false,
    notes        text,
    added_by     uuid,
    reviewed_by  uuid,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, domain),
    -- Registrable hostname only: no scheme, no path, no port, no wildcard.
    -- The fetcher matches host == domain OR host ends with '.' || domain, so
    -- a malformed row cannot widen the allowlist by accident.
    CONSTRAINT web_domains_hostname_shape CHECK (domain ~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$')
);

CREATE INDEX web_domains_lookup_idx ON web_domains (tenant_id, status, domain);

-- ---------------------------------------------------------------- page cache

CREATE TABLE web_pages (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL,
    -- sha256 of the normalized URL; the natural key (URLs exceed index limits).
    url_hash      text NOT NULL,
    url           text NOT NULL,
    domain        text NOT NULL,
    title         text,
    fetched_at    timestamptz NOT NULL DEFAULT now(),
    robots_ok     boolean NOT NULL DEFAULT true,
    -- What we may do with the body: 'open' (extract + quote),
    -- 'metadata_only' (cite title/URL only), 'unknown' (treated as metadata_only).
    license_flag  text NOT NULL DEFAULT 'unknown' CHECK (license_flag IN
                    ('open', 'metadata_only', 'unknown')),
    status        text NOT NULL CHECK (status IN
                    ('ok', 'paywalled', 'robots_skip', 'garbage', 'quarantined', 'error')),
    http_status   integer,
    content_type  text,
    byte_size     integer,
    -- Object keys in the evidence bucket (via libs/storage, rule E3).
    snapshot_ref  text,
    extract_ref   text,
    -- Why a non-ok page was rejected; surfaces in the answer trace.
    skip_reason   text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, url_hash)
);

CREATE INDEX web_pages_domain_idx ON web_pages (tenant_id, domain, fetched_at DESC);
CREATE INDEX web_pages_fetched_idx ON web_pages (tenant_id, fetched_at DESC);

-- ---------------------------------------------------------------- RLS

-- web_domains: tenant rows are fully managed by the tenant; the shipped
-- GLOBAL rows are readable by everyone and writable by nobody but an
-- operator scoped to the nil uuid (same discipline as the global corpus).
ALTER TABLE web_domains ENABLE ROW LEVEL SECURITY;
ALTER TABLE web_domains FORCE  ROW LEVEL SECURITY;
CREATE POLICY web_domains_tenant_select ON web_domains
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid
           OR tenant_id = '00000000-0000-0000-0000-000000000000'::uuid);
CREATE POLICY web_domains_tenant_insert ON web_domains
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY web_domains_tenant_update ON web_domains
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY web_domains_tenant_delete ON web_domains
    FOR DELETE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY web_domains_tenant_restrictive ON web_domains
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid
           OR tenant_id = '00000000-0000-0000-0000-000000000000'::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON web_domains TO app_role;

-- web_pages: strictly tenant-scoped. A cached page is tenant data (which
-- tenant looked at what is itself information), so no global read here.
ALTER TABLE web_pages ENABLE ROW LEVEL SECURITY;
ALTER TABLE web_pages FORCE  ROW LEVEL SECURITY;
CREATE POLICY web_pages_tenant_select ON web_pages
    FOR SELECT TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY web_pages_tenant_insert ON web_pages
    FOR INSERT TO app_role
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY web_pages_tenant_update ON web_pages
    FOR UPDATE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY web_pages_tenant_delete ON web_pages
    FOR DELETE TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY web_pages_tenant_restrictive ON web_pages
    AS RESTRICTIVE FOR ALL TO app_role
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON web_pages TO app_role;

-- ---------------------------------------------------------------- seed

-- Shipped allowlist v1 — the reviewed table is docs/corpus/web-domains.md in
-- evidence-backend; changing this set is a doc + clinical-review change, not
-- a quiet SQL edit. Seeded under the reserved GLOBAL tenant.
INSERT INTO web_domains (tenant_id, domain, trust_tier, status, is_default, metadata_only, notes)
VALUES
    -- International organizations
    ('00000000-0000-0000-0000-000000000000', 'who.int', 'international_organization', 'enabled', true, false, 'WHO — guidelines, fact sheets'),
    ('00000000-0000-0000-0000-000000000000', 'iris.who.int', 'international_organization', 'enabled', true, false, 'WHO institutional repository (full guideline PDFs/HTML)'),
    ('00000000-0000-0000-0000-000000000000', 'ecdc.europa.eu', 'international_organization', 'enabled', true, false, 'European Centre for Disease Prevention and Control'),
    ('00000000-0000-0000-0000-000000000000', 'ema.europa.eu', 'international_organization', 'enabled', true, false, 'European Medicines Agency — SmPCs, safety communications'),
    -- Government / state agencies
    ('00000000-0000-0000-0000-000000000000', 'cdc.gov', 'government', 'enabled', true, false, 'US CDC'),
    ('00000000-0000-0000-0000-000000000000', 'fda.gov', 'government', 'enabled', true, false, 'US FDA — labels, safety communications'),
    ('00000000-0000-0000-0000-000000000000', 'nice.org.uk', 'government', 'enabled', true, false, 'NICE (UK) — guidance'),
    ('00000000-0000-0000-0000-000000000000', 'moz.gov.ua', 'government', 'enabled', true, false, 'МОЗ України — накази, клінічні настанови'),
    ('00000000-0000-0000-0000-000000000000', 'dec.gov.ua', 'government', 'enabled', true, false, 'ДЕЦ МОЗ — державний реєстр лікарських засобів, настанови'),
    ('00000000-0000-0000-0000-000000000000', 'pubmed.ncbi.nlm.nih.gov', 'government', 'enabled', true, true, 'PubMed abstract pages — metadata + abstract only'),
    ('00000000-0000-0000-0000-000000000000', 'ncbi.nlm.nih.gov', 'government', 'enabled', true, false, 'PMC open-access articles, Bookshelf'),
    -- Guideline registries
    ('00000000-0000-0000-0000-000000000000', 'g-i-n.net', 'guideline_registry', 'enabled', true, false, 'Guidelines International Network'),
    ('00000000-0000-0000-0000-000000000000', 'magicevidence.org', 'guideline_registry', 'enabled', true, false, 'MAGICapp published guidelines'),
    -- Professional societies
    ('00000000-0000-0000-0000-000000000000', 'escardio.org', 'professional_society', 'enabled', true, false, 'European Society of Cardiology'),
    ('00000000-0000-0000-0000-000000000000', 'acc.org', 'professional_society', 'enabled', true, false, 'American College of Cardiology'),
    ('00000000-0000-0000-0000-000000000000', 'idsociety.org', 'professional_society', 'enabled', true, false, 'Infectious Diseases Society of America'),
    ('00000000-0000-0000-0000-000000000000', 'ersnet.org', 'professional_society', 'enabled', true, false, 'European Respiratory Society'),
    ('00000000-0000-0000-0000-000000000000', 'easl.eu', 'professional_society', 'enabled', true, false, 'European Association for the Study of the Liver'),
    ('00000000-0000-0000-0000-000000000000', 'kdigo.org', 'professional_society', 'enabled', true, false, 'KDIGO — kidney disease guidelines'),
    ('00000000-0000-0000-0000-000000000000', 'ginasthma.org', 'professional_society', 'enabled', true, false, 'Global Initiative for Asthma'),
    ('00000000-0000-0000-0000-000000000000', 'goldcopd.org', 'professional_society', 'enabled', true, false, 'Global Initiative for Chronic Obstructive Lung Disease'),
    ('00000000-0000-0000-0000-000000000000', 'diabetes.org', 'professional_society', 'enabled', true, false, 'American Diabetes Association — Standards of Care'),
    -- Journals (mostly paywalled: metadata-only citation, §7)
    ('00000000-0000-0000-0000-000000000000', 'cochranelibrary.com', 'journal', 'enabled', true, true, 'Cochrane systematic reviews — abstract/metadata only'),
    ('00000000-0000-0000-0000-000000000000', 'bmj.com', 'journal', 'enabled', true, true, 'BMJ — metadata only'),
    ('00000000-0000-0000-0000-000000000000', 'thelancet.com', 'journal', 'enabled', true, true, 'The Lancet — metadata only'),
    ('00000000-0000-0000-0000-000000000000', 'nejm.org', 'journal', 'enabled', true, true, 'NEJM — metadata only')
ON CONFLICT (tenant_id, domain) DO NOTHING;
