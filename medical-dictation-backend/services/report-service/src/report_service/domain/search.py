"""Search query construction + cursor pagination + snippet generation.

Postgres ``simple`` FTS config (ADR-0021). Composes filter clauses
with AND. Joins to ``report_versions`` so the search hits the current
version's rendered text. Snippet via ``ts_headline`` is run inside the
same query for one DB round-trip.

Cursor encoding (opaque to clients): base64 url-safe of the tuple
``(encounter_date_iso_or_empty, report_id_hex)``. Tie-break by id so
the cursor is stable.
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


@dataclass(slots=True)
class SearchHit:
    report_id: UUID
    code: str
    title: str
    status: str
    encounter_date: date | None
    primary_author_id: UUID
    co_author_ids: list[UUID]
    icd10_codes: list[str]
    snippet: str
    updated_at: datetime


def encode_cursor(*, encounter_date: date | None, report_id: UUID) -> str:
    payload = {
        "d": encounter_date.isoformat() if encounter_date else "",
        "i": report_id.hex,
    }
    return base64.urlsafe_b64encode(json.dumps(payload).encode("ascii")).decode("ascii")


def decode_cursor(value: str) -> tuple[date | None, UUID]:
    raw = base64.urlsafe_b64decode(value.encode("ascii"))
    obj = json.loads(raw.decode("ascii"))
    d = date.fromisoformat(obj["d"]) if obj["d"] else None
    return d, UUID(obj["i"])


async def search_reports(
    conn: asyncpg.Connection,
    *,
    filters: SearchFilters,
    limit: int,
    cursor: tuple[date | None, UUID] | None,
) -> tuple[list[SearchHit], str | None, int | None]:
    """Run the FTS + filters query.

    Returns (hits, next_cursor, total_estimated).
    """
    where: list[str] = []
    args: list[Any] = []
    fts_arg_idx: int | None = None

    if filters.q:
        args.append(filters.q)
        fts_arg_idx = len(args)
        where.append(f"v.search_vector @@ plainto_tsquery('simple', ${fts_arg_idx})")

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
        cur_d, cur_id = cursor
        # Order: encounter_date DESC NULLS LAST, id DESC.
        if cur_d is None:
            args.append(cur_id)
            where.append(f"r.id < ${len(args)} AND r.encounter_date IS NULL")
        else:
            args.append(cur_d)
            args.append(cur_id)
            where.append(
                f"(r.encounter_date < ${len(args) - 1} "
                f" OR (r.encounter_date = ${len(args) - 1} AND r.id < ${len(args)}))"
            )

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    args.append(limit + 1)

    snippet_expr = (
        f"ts_headline('simple', v.rendered_text, plainto_tsquery('simple', ${fts_arg_idx}), "
        f"'MaxFragments=2, MaxWords=15, MinWords=5, StartSel=<mark>, StopSel=</mark>')"
        if fts_arg_idx is not None
        else "''"
    )

    sql = f"""
        SELECT
            r.id, r.code, r.title, r.status, r.encounter_date,
            r.primary_author_id, r.co_author_ids, r.icd10_codes,
            r.updated_at,
            {snippet_expr} AS snippet
        FROM reports r
        JOIN report_versions v ON v.id = r.current_version_id
        {where_sql}
        ORDER BY r.encounter_date DESC NULLS LAST, r.id DESC
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
                encounter_date=r["encounter_date"],
                primary_author_id=r["primary_author_id"],
                co_author_ids=list(r["co_author_ids"] or []),
                icd10_codes=list(r["icd10_codes"] or []),
                snippet=r["snippet"] or "",
                updated_at=r["updated_at"],
            )
        )
    next_cursor: str | None = None
    if has_more and hits:
        last = hits[-1]
        next_cursor = encode_cursor(encounter_date=last.encounter_date, report_id=last.report_id)
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
    if filters.q:
        args.append(filters.q)
        where.append(f"v.search_vector @@ plainto_tsquery('simple', ${len(args)})")
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
