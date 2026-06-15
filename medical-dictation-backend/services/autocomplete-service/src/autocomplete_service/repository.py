"""Repository — SQL wrappers over phrases / snippets / telemetry.

Every tenant-scoped query runs on an asyncpg connection that has
already had ``app.tenant_id`` + ``app.user_id`` + ``app.user_role``
set by the dependency layer (sprint-10 day-1: RLS depends on these
three GUC keys for the ``write_user_phrases`` policy).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from autocomplete_service.trie.builder import PhraseTrieEntry

logger = logging.getLogger(__name__)


# ── Phrases (corpus pull) ───────────────────────────────────────────


async def fetch_corpus(
    conn: asyncpg.Connection,
    *,
    language: str,
) -> list[PhraseTrieEntry]:
    rows = await conn.fetch(
        """
        SELECT id::text, phrase, source::text,
               impression_count, acceptance_count, last_accepted_at,
               specialty, section_hint
        FROM autocomplete_phrases
        WHERE language = $1
          AND enabled  = TRUE
          AND (
              source = 'system'
              OR tenant_id = current_setting('app.tenant_id', true)::uuid
          )
        """,
        language,
    )
    out: list[PhraseTrieEntry] = []
    for r in rows:
        out.append(
            PhraseTrieEntry(
                id=r["id"],
                phrase=r["phrase"],
                source=r["source"],
                impression_count=int(r["impression_count"]),
                acceptance_count=int(r["acceptance_count"]),
                last_accepted_at=r["last_accepted_at"],
                specialty=r["specialty"],
                section_hint=r["section_hint"],
            )
        )
    return out


async def insert_phrase(
    conn: asyncpg.Connection,
    *,
    phrase: str,
    language: str,
    specialty: str | None,
    section_hint: str | None,
    source: str,
    tenant_id: UUID,
    owner_user_id: UUID | None,
) -> UUID:
    return await conn.fetchval(
        """
        INSERT INTO autocomplete_phrases
            (tenant_id, owner_user_id, phrase, language, specialty, section_hint, source)
        VALUES ($1, $2, $3, $4, $5, $6, $7::autocomplete_source)
        RETURNING id
        """,
        tenant_id,
        owner_user_id,
        phrase,
        language,
        specialty,
        section_hint,
        source,
    )


async def soft_delete_phrase(conn: asyncpg.Connection, *, phrase_id: UUID) -> bool:
    row = await conn.fetchrow(
        "UPDATE autocomplete_phrases SET enabled = FALSE, updated_at = now() "
        "WHERE id = $1 RETURNING id",
        phrase_id,
    )
    return row is not None


async def list_phrases(
    conn: asyncpg.Connection,
    *,
    language: str | None,
    specialty: str | None,
    source: str | None,
    limit: int,
) -> list[asyncpg.Record]:
    sql_parts = [
        "SELECT id, phrase, language, specialty, section_hint, source, "
        "impression_count, acceptance_count, enabled, created_at "
        "FROM autocomplete_phrases WHERE enabled = TRUE"
    ]
    args: list[Any] = []
    if language:
        args.append(language)
        sql_parts.append(f"AND language = ${len(args)}")
    if specialty:
        args.append(specialty)
        sql_parts.append(f"AND specialty = ${len(args)}")
    if source:
        args.append(source)
        sql_parts.append(f"AND source = ${len(args)}::autocomplete_source")
    args.append(limit)
    sql_parts.append(f"ORDER BY updated_at DESC LIMIT ${len(args)}")
    return list(await conn.fetch(" ".join(sql_parts), *args))


# ── Snippets ────────────────────────────────────────────────────────


async def fetch_snippet(
    conn: asyncpg.Connection, *, trigger: str, language: str
) -> asyncpg.Record | None:
    """Resolve trigger with user→tenant→system fallback.

    The visibility policy ensures we only see rows in scope; the
    explicit ORDER BY enforces the precedence.
    """
    return await conn.fetchrow(
        """
        SELECT id, expansion, cursor_position, source
        FROM autocomplete_snippets
        WHERE enabled = TRUE
          AND language = $2
          AND trigger  = $1
        ORDER BY
            CASE source
                WHEN 'user'   THEN 0
                WHEN 'tenant' THEN 1
                WHEN 'system' THEN 2
                ELSE 3
            END
        LIMIT 1
        """,
        trigger,
        language,
    )


async def insert_snippet(
    conn: asyncpg.Connection,
    *,
    trigger: str,
    expansion: str,
    cursor_position: int,
    language: str,
    source: str,
    tenant_id: UUID,
    owner_user_id: UUID | None,
) -> UUID:
    return await conn.fetchval(
        """
        INSERT INTO autocomplete_snippets
            (tenant_id, owner_user_id, trigger, expansion, cursor_position, language, source)
        VALUES ($1, $2, $3, $4, $5, $6, $7::autocomplete_source)
        RETURNING id
        """,
        tenant_id,
        owner_user_id,
        trigger,
        expansion,
        cursor_position,
        language,
        source,
    )


# ── Telemetry ──────────────────────────────────────────────────────


async def insert_telemetry_batch(conn: asyncpg.Connection, rows: list[tuple]) -> int:
    if not rows:
        return 0
    await conn.executemany(
        """
        INSERT INTO autocomplete_telemetry
            (tenant_id, user_id, request_id, event_type,
             phrase_id, snippet_id, prefix_scrubbed, context_jsonb)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
        """,
        rows,
    )
    return len(rows)


async def rollup_tenant_day(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    day_iso: str,
) -> int:
    """Aggregate one tenant's telemetry for one day into phrase counters.

    Returns the number of phrase rows updated.
    Idempotent via the ``autocomplete_rollup_progress`` table.
    """
    row = await conn.fetchrow(
        "SELECT 1 FROM autocomplete_rollup_progress WHERE rollup_date = $1::date AND tenant_id = $2",
        day_iso,
        tenant_id,
    )
    if row is not None:
        return 0

    impressions = await conn.fetch(
        """
        SELECT phrase_id,
               COUNT(*) FILTER (WHERE event_type IN ('shown_only','accepted','rejected')) AS impressions,
               COUNT(*) FILTER (WHERE event_type = 'accepted') AS accepts,
               MAX(created_at) FILTER (WHERE event_type = 'accepted') AS last_acc
        FROM autocomplete_telemetry
        WHERE tenant_id = $1
          AND phrase_id IS NOT NULL
          AND created_at >= $2::date
          AND created_at <  ($2::date + interval '1 day')
        GROUP BY phrase_id
        """,
        tenant_id,
        day_iso,
    )
    updated = 0
    for r in impressions:
        await conn.execute(
            """
            UPDATE autocomplete_phrases
            SET impression_count = impression_count + $2,
                acceptance_count = acceptance_count + $3,
                last_accepted_at = COALESCE($4, last_accepted_at),
                updated_at       = now()
            WHERE id = $1
            """,
            r["phrase_id"],
            int(r["impressions"]),
            int(r["accepts"]),
            r["last_acc"],
        )
        updated += 1

    await conn.execute(
        "INSERT INTO autocomplete_rollup_progress (rollup_date, tenant_id, events_processed) "
        "VALUES ($1::date, $2, $3)",
        day_iso,
        tenant_id,
        sum(int(r["impressions"]) for r in impressions),
    )
    return updated


async def create_next_telemetry_partition(
    conn: asyncpg.Connection, *, start: datetime, end: datetime
) -> str:
    """Idempotent partition creation. ``start`` and ``end`` are
    timezone-aware datetimes at month boundaries."""
    name = f"autocomplete_telemetry_{start.strftime('%Y_%m')}"
    await conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {name}
        PARTITION OF autocomplete_telemetry
        FOR VALUES FROM ('{start.date().isoformat()}') TO ('{end.date().isoformat()}')
        """
    )
    return name
