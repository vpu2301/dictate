"""Sprint-15 query expansion — live-DB contract (RUN_DB_INTEGRATION=1).

Covers the VERIFY matrix:
  * «ІМ» finds a report containing only «інфаркт міокарда» — and vice versa
  * expand=false (no ts_query) does NOT
  * snippet still carries <mark> highlights under the expanded tsquery
  * medical_synonyms RLS: tenant A never sees B's rows; system rows visible
    to all; app_role cannot write system rows
  * EXPLAIN: the expanded to_tsquery still hits the GIN bitmap plan
    (first-of-kind plan-shape regression test)

Needs `make dev-up && make migrate-up && make seed` (tenants, templates).
"""

from __future__ import annotations

import json
import os
from uuid import UUID, uuid4

import asyncpg
import pytest

from db import create_pool, tenant_connection

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_INTEGRATION") != "1",
    reason="set RUN_DB_INTEGRATION=1; needs dev-up + migrate-up + seed",
)

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))
DB_NAME = os.environ.get("POSTGRES_DB", "medical_dictation")
SU_DSN = f"postgresql://postgres:postgres@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
APP_DSN = f"postgresql://app_role:app_role@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"

TENANT_A = UUID("00000000-0000-0000-0000-00000000000a")
TENANT_B = UUID("00000000-0000-0000-0000-00000000000b")
MARK = "itest-syn"


async def _seed_report(su: asyncpg.Connection, *, tenant: UUID, text: str) -> UUID:
    """Minimal report + current version whose rendered_text carries `text`."""
    clinician = await su.fetchval(
        "SELECT sub FROM users WHERE tenant_id=$1 LIMIT 1", tenant
    )
    template_id = await su.fetchval("SELECT id FROM templates LIMIT 1")
    patient_id = await su.fetchval(
        "SELECT id FROM patients WHERE tenant_id=$1 LIMIT 1", tenant
    )
    assert clinician and template_id and patient_id, "run `make seed` first"
    report_id, version_id = uuid4(), uuid4()
    await su.execute(
        "INSERT INTO reports (id, tenant_id, code, title, status, template_id,"
        " primary_author_id, co_author_ids, patient_id)"
        " VALUES ($1,$2,$3,$4,'draft',$5,$6,'{}',$7)",
        report_id, tenant, f"{MARK}-{str(report_id)[:8]}", f"{MARK} report",
        template_id, clinician, patient_id,
    )
    content = {"template_id": str(template_id), "language": "uk", "sections": []}
    await su.execute(
        "INSERT INTO report_versions (id, report_id, version_number, created_by,"
        " content_jsonb, rendered_text)"
        " VALUES ($1,$2,1,$3,$4,$5)",
        version_id, report_id, clinician, json.dumps(content), text,
    )
    await su.execute(
        "UPDATE reports SET current_version_id=$2 WHERE id=$1", report_id, version_id
    )
    return report_id


async def _search_ids(pool, tenant: UUID, q: str, *, expand: bool) -> list[UUID]:
    from report_service.domain import query_expansion
    from report_service.domain import search as searchmod

    filters = searchmod.SearchFilters(q=q)
    async with tenant_connection(pool, tenant) as conn:
        if expand:
            expansion = await query_expansion.expand_query(conn, raw_q=q)
            if expansion.tsquery is not None and expansion.groups_used > 0:
                filters.ts_query = expansion.tsquery
        hits, _, _ = await searchmod.search_reports(
            conn, filters=filters, limit=50, cursor=None
        )
    return [h.report_id for h in hits]


async def test_expansion_both_directions_and_bypass_and_snippet():
    su = await asyncpg.connect(SU_DSN)
    pool = await create_pool(APP_DSN, application_name="itest-syn", min_size=1, max_size=2)
    try:
        full_form = await _seed_report(
            su, tenant=TENANT_A,
            text=f"{MARK} Пацієнт переніс інфаркт міокарда у 2024 році.",
        )
        abbrev = await _seed_report(
            su, tenant=TENANT_A, text=f"{MARK} В анамнезі ІМ, стентування ЛКА."
        )

        # «ІМ» finds the report that only says «інфаркт міокарда» …
        ids = await _search_ids(pool, TENANT_A, "ІМ", expand=True)
        assert full_form in ids and abbrev in ids
        # … and vice versa.
        ids = await _search_ids(pool, TENANT_A, "інфаркт міокарда", expand=True)
        assert abbrev in ids and full_form in ids
        # expand=false: exact terms only.
        ids = await _search_ids(pool, TENANT_A, "ІМ", expand=False)
        assert abbrev in ids and full_form not in ids

        # Snippet keeps <mark> highlights under the expanded tsquery.
        from report_service.domain import query_expansion
        from report_service.domain import search as searchmod

        filters = searchmod.SearchFilters(q="ІМ")
        async with tenant_connection(pool, TENANT_A) as conn:
            expansion = await query_expansion.expand_query(conn, raw_q="ІМ")
            assert expansion.groups_used >= 1
            assert "інфаркт міокарда" in expansion.expanded_terms
            filters.ts_query = expansion.tsquery
            hits, _, _ = await searchmod.search_reports(
                conn, filters=filters, limit=50, cursor=None
            )
        by_id = {h.report_id: h for h in hits}
        assert "<mark>" in by_id[full_form].snippet
        assert "<mark>" in by_id[abbrev].snippet
    finally:
        await su.execute(
            "UPDATE reports SET current_version_id=NULL WHERE title LIKE $1", f"{MARK}%"
        )
        await su.execute(
            "DELETE FROM report_versions WHERE report_id IN "
            "(SELECT id FROM reports WHERE title LIKE $1)", f"{MARK}%",
        )
        await su.execute("DELETE FROM reports WHERE title LIKE $1", f"{MARK}%")
        await su.close()
        await pool.close()


async def test_explain_expanded_query_plan_shapes():
    """First-of-kind plan-shape regression, two honest halves.

    (1) The OR-grouped expanded tsquery is GIN-INDEXABLE: with RLS out of
    the way it hits report_versions_search_vector_idx via a Bitmap Index
    Scan — the exact regression ADR-0021 worried about ("OR groups do").
    Measured WITHOUT RLS because `@@` (ts_match_vq) is not leakproof and
    Postgres therefore refuses to push it into an index condition ahead
    of row-security quals — discovered by this sprint: the sprint-08
    "canonical GIN plan" (docs/eval/sprint-08-loadtest.md) was captured
    as superuser, and app_role's RLS plan has ALWAYS been the
    tenant-driven join below.

    (2) Under app_role/RLS the REAL search query keeps the tenant-first
    Nested Loop shape (reports by tenant index → versions by pkey, FTS
    as filter) with expansion applied — i.e. expansion does not degrade
    the production plan.
    """
    from report_service.domain import query_expansion

    pool = await create_pool(APP_DSN, application_name="itest-explain", min_size=1, max_size=2)
    su = await asyncpg.connect(SU_DSN)
    try:
        async with tenant_connection(pool, TENANT_A) as conn:
            expansion = await query_expansion.expand_query(conn, raw_q="ІМ")
            assert expansion.tsquery is not None
            assert expansion.groups_used >= 1, "seed groups must fire for ІМ"
            assert " | " in expansion.tsquery  # real OR-expansion happened

            # (2) production plan under RLS: tenant-driven, no Seq Scan
            # over reports, FTS applied as a filter.
            await conn.execute("SET LOCAL enable_seqscan = off")
            rls_plan = await conn.fetchval(
                "EXPLAIN (FORMAT JSON) "
                "SELECT r.id FROM reports r "
                "JOIN report_versions v ON v.id = r.current_version_id "
                "WHERE v.search_vector @@ to_tsquery('simple', $1)",
                expansion.tsquery,
            )
        rls_text = json.dumps(json.loads(rls_plan))
        assert '"Relation Name": "reports"' in rls_text
        assert "search_vector" in rls_text  # the FTS predicate survived

        # (1) index-path proof for the expanded tsquery shape.
        await su.execute("SET enable_seqscan = off")
        plan_json = await su.fetchval(
            "EXPLAIN (FORMAT JSON) "
            "SELECT v.id FROM report_versions v "
            "WHERE v.search_vector @@ to_tsquery('simple', $1)",
            expansion.tsquery,
        )
        plan_text = json.dumps(json.loads(plan_json))
        assert "Bitmap Index Scan" in plan_text, plan_text
        assert "report_versions_search_vector_idx" in plan_text, plan_text
        assert '"Node Type": "Seq Scan"' not in plan_text, plan_text
        print("\nEXPLAIN (expanded tsquery, GIN path):\n", plan_text[:1600])
        print("\nEXPLAIN (RLS production shape):\n", rls_text[:1600])
    finally:
        await su.close()
        await pool.close()


async def test_medical_synonyms_rls_isolation():
    su = await asyncpg.connect(SU_DSN)
    pool = await create_pool(APP_DSN, application_name="itest-syn-rls", min_size=1, max_size=2)
    group_b = uuid4()
    try:
        # Tenant B private group, inserted as B through RLS.
        async with tenant_connection(pool, TENANT_B) as conn:
            await conn.execute(
                "INSERT INTO medical_synonyms (tenant_id, group_id, term, lexemes,"
                " language, source) VALUES "
                "($1, $2, $3, tsvector_to_array(to_tsvector('simple', $3)), 'uk', 'tenant'),"
                "($1, $2, $4, tsvector_to_array(to_tsvector('simple', $4)), 'uk', 'tenant')",
                TENANT_B, group_b, f"{MARK}-БТЕРМ", f"{MARK}-повна форма бе",
            )

        # System rows visible to both tenants.
        for tenant in (TENANT_A, TENANT_B):
            async with tenant_connection(pool, tenant) as conn:
                n = await conn.fetchval(
                    "SELECT count(*) FROM medical_synonyms WHERE source='system'"
                )
            assert n >= 400, f"system seed missing for {tenant}"

        # Tenant A cannot see B's rows.
        async with tenant_connection(pool, TENANT_A) as conn:
            n = await conn.fetchval(
                "SELECT count(*) FROM medical_synonyms WHERE group_id = $1", group_b
            )
        assert n == 0

        # app_role cannot write system rows (no PERMISSIVE write policy).
        # Separate connections: the refused INSERT aborts its transaction,
        # so the DELETE probe needs a fresh one.
        async with tenant_connection(pool, TENANT_A) as conn:
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await conn.execute(
                    "INSERT INTO medical_synonyms (tenant_id, group_id, term, lexemes,"
                    " language, source) VALUES (NULL, $1, $2, ARRAY['x'], 'uk', 'system')",
                    uuid4(), f"{MARK}-sys",
                )
        async with tenant_connection(pool, TENANT_A) as conn:
            # …and cannot UPDATE/DELETE them either (0 rows affected, no error).
            tag = await conn.execute(
                "DELETE FROM medical_synonyms WHERE source='system' AND term='ІМ'"
            )
            assert tag == "DELETE 0"
        n = await su.fetchval(
            "SELECT count(*) FROM medical_synonyms WHERE source='system' AND term='ІМ'"
        )
        assert n == 1  # still there
    finally:
        await su.execute(
            "DELETE FROM medical_synonyms WHERE term LIKE $1", f"{MARK}%"
        )
        await su.close()
        await pool.close()
