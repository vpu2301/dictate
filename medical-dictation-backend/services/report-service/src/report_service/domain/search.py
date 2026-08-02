"""Search query construction + cursor pagination + snippet generation.

Postgres ``simple`` FTS config (ADR-0021). Composes filter clauses
with AND. Joins to ``report_versions`` so the search hits the current
version's rendered text. Snippet via ``ts_headline`` is run inside the
same query for one DB round-trip.

Results are ordered most-recent-first by ``created_at`` (a monotonic,
never-NULL column) with ``id`` as a stable tie-break. Earlier revisions
ordered by ``encounter_date DESC NULLS LAST, id DESC``; because dictation
drafts never set ``encounter_date`` (it stays NULL until finalize), that
degenerated into ordering by the random ``id`` UUID, so a freshly-created
draft could land arbitrarily deep in the list and fall outside the first
page — making it look like nothing was stored.

Cursor encoding (opaque to clients): base64 url-safe of the tuple
``(created_at_iso, report_id_hex)``. Tie-break by id so the cursor is
stable.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

import asyncpg


@dataclass(slots=True)
class SearchFilters:
    q: str | None = None
    patient_id: UUID | None = None
    author_id: UUID | None = None
    statuses: list[str] | None = None
    encounter_date_from: date | None = None
    encounter_date_to: date | None = None
    icd10: list[str] | None = None
    # Sprint 15 (ADR-0038): pre-assembled tsquery string from
    # domain/query_expansion — when set (and q is set) the FTS tier uses
    # to_tsquery('simple', ts_query) instead of plainto over q. The SAME
    # bind arg feeds the predicate, ts_headline AND exact_total; unset →
    # the pre-sprint-15 plainto path, byte-identical.
    ts_query: str | None = None


def _fts_clause(filters: SearchFilters, args: list[Any]) -> tuple[str, str] | None:
    """Append the FTS bind arg; return (predicate, tsquery_expr) or None.

    ONE place builds the tsquery expression so the match predicate,
    the snippet highlighter and the exact count can never disagree.
    """
    if not filters.q:
        return None
    if filters.ts_query is not None:
        args.append(filters.ts_query)
        expr = f"to_tsquery('simple', ${len(args)})"
    else:
        args.append(filters.q)
        expr = f"plainto_tsquery('simple', ${len(args)})"
    return f"v.search_vector @@ {expr}", expr


@dataclass(slots=True)
class SearchHit:
    report_id: UUID
    code: str
    title: str
    status: str
    template_id: UUID
    encounter_date: date | None
    primary_author_id: UUID
    co_author_ids: list[UUID]
    patient_id: UUID | None
    patient_name_redacted: str | None
    icd10_codes: list[str]
    snippet: str
    created_at: datetime
    updated_at: datetime


def encode_cursor(*, created_at: datetime, report_id: UUID) -> str:
    payload = {
        "c": created_at.isoformat(),
        "i": report_id.hex,
    }
    return base64.urlsafe_b64encode(json.dumps(payload).encode("ascii")).decode("ascii")


def decode_cursor(value: str) -> tuple[datetime, UUID]:
    raw = base64.urlsafe_b64decode(value.encode("ascii"))
    obj = json.loads(raw.decode("ascii"))
    return datetime.fromisoformat(obj["c"]), UUID(obj["i"])


async def search_reports(
    conn: asyncpg.Connection,
    *,
    filters: SearchFilters,
    limit: int,
    cursor: tuple[datetime, UUID] | None,
) -> tuple[list[SearchHit], str | None, int | None]:
    """Run the FTS + filters query.

    Returns (hits, next_cursor, total_estimated).
    """
    where: list[str] = []
    args: list[Any] = []
    tsquery_expr: str | None = None

    fts = _fts_clause(filters, args)
    if fts is not None:
        predicate, tsquery_expr = fts
        where.append(predicate)

    if filters.patient_id is not None:
        args.append(filters.patient_id)
        where.append(f"r.patient_id = ${len(args)}")

    if filters.author_id is not None:
        args.append(filters.author_id)
        where.append(f"(r.primary_author_id = ${len(args)} OR ${len(args)} = ANY(r.co_author_ids))")

    if filters.statuses:
        args.append(filters.statuses)
        where.append(f"r.status = ANY(${len(args)}::report_status[])")

    if filters.encounter_date_from is not None:
        args.append(filters.encounter_date_from)
        where.append(f"r.encounter_date >= ${len(args)}")

    if filters.encounter_date_to is not None:
        args.append(filters.encounter_date_to)
        where.append(f"r.encounter_date <= ${len(args)}")

    if filters.icd10:
        args.append(filters.icd10)
        where.append(f"r.icd10_codes && ${len(args)}::text[]")

    if cursor is not None:
        cur_c, cur_id = cursor
        # Order: created_at DESC, id DESC — keyset predicate for "strictly after".
        args.append(cur_c)
        args.append(cur_id)
        where.append(
            f"(r.created_at < ${len(args) - 1} "
            f" OR (r.created_at = ${len(args) - 1} AND r.id < ${len(args)}))"
        )

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    args.append(limit + 1)

    snippet_expr = (
        f"ts_headline('simple', v.rendered_text, {tsquery_expr}, "
        f"'MaxFragments=2, MaxWords=15, MinWords=5, StartSel=<mark>, StopSel=</mark>')"
        if tsquery_expr is not None
        else "''"
    )

    sql = f"""
        SELECT
            r.id, r.code, r.title, r.status, r.template_id, r.encounter_date,
            r.primary_author_id, r.co_author_ids,
            r.patient_id, r.patient_name_redacted, r.icd10_codes,
            r.created_at, r.updated_at,
            {snippet_expr} AS snippet
        FROM reports r
        JOIN report_versions v ON v.id = r.current_version_id
        {where_sql}
        ORDER BY r.created_at DESC, r.id DESC
        LIMIT ${len(args)}
    """
    rows = await conn.fetch(sql, *args)

    has_more = len(rows) > limit
    rows = rows[:limit]
    hits: list[SearchHit] = []
    for r in rows:
        hits.append(
            SearchHit(
                report_id=r["id"],
                code=r["code"],
                title=r["title"],
                status=r["status"],
                template_id=r["template_id"],
                encounter_date=r["encounter_date"],
                primary_author_id=r["primary_author_id"],
                co_author_ids=list(r["co_author_ids"] or []),
                patient_id=r["patient_id"],
                patient_name_redacted=r["patient_name_redacted"],
                icd10_codes=list(r["icd10_codes"] or []),
                snippet=r["snippet"] or "",
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
        )
    next_cursor: str | None = None
    if has_more and hits:
        last = hits[-1]
        next_cursor = encode_cursor(created_at=last.created_at, report_id=last.report_id)
    total_estimated = await _estimate_total(conn)
    return hits, next_cursor, total_estimated


async def _estimate_total(conn: asyncpg.Connection) -> int:
    """Cheap reltuples-based estimate. Caller can opt into ``total=exact``
    via the router; that path bypasses this helper."""
    val = await conn.fetchval("SELECT reltuples::bigint FROM pg_class WHERE relname = 'reports'")
    return int(val or 0)


async def exact_total(conn: asyncpg.Connection, filters: SearchFilters) -> int:
    """Slow path; rate-limited at router layer."""
    where: list[str] = []
    args: list[Any] = []
    fts = _fts_clause(filters, args)
    if fts is not None:
        where.append(fts[0])
    if filters.statuses:
        args.append(filters.statuses)
        where.append(f"r.status = ANY(${len(args)}::report_status[])")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    val = await conn.fetchval(
        f"""
        SELECT count(*) FROM reports r
        JOIN report_versions v ON v.id = r.current_version_id
        {where_sql}
        """,
        *args,
    )
    return int(val or 0)
