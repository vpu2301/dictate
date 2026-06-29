"""Repository helpers for nlp-service.

Loads the voice command catalogue + abbreviation snapshot. All
abbreviation reads are RLS-scoped via :func:`db.tenant_connection` —
tenant rows see their own + global rows; cross-tenant access is
impossible at the DB layer.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any
from uuid import UUID

import asyncpg

from ..pipeline.base import AbbreviationEntry, AbbreviationSnapshot
from ..stages import CommandSpec

logger = logging.getLogger(__name__)


async def load_voice_commands(
    pool: asyncpg.Pool,
) -> dict[str, list[CommandSpec]]:
    """Read voice_commands table → indexed by language.

    Falls back to an empty catalogue if the table is absent (dev hosts
    without migrations). The matcher tolerates an empty catalogue.
    """
    out: dict[str, list[CommandSpec]] = {"uk": [], "en": []}
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT intent, language, phrases, requires_pause_before_ms,
                       min_avg_probability, is_section_command
                FROM voice_commands
                WHERE is_active = TRUE
                """,
            )
    except asyncpg.UndefinedTableError:
        logger.warning("voice_commands.table_missing")
        return out

    for row in rows:
        phrases_raw = row["phrases"]
        if isinstance(phrases_raw, str):
            phrases_raw = json.loads(phrases_raw)
        phrases = tuple(tuple(p) for p in phrases_raw)
        spec = CommandSpec(
            intent=row["intent"],
            language=row["language"],
            phrases=phrases,
            requires_pause_before_ms=int(row["requires_pause_before_ms"]),
            min_avg_probability=float(row["min_avg_probability"]),
            is_section_command=bool(row["is_section_command"]),
        )
        out.setdefault(spec.language, []).append(spec)
    return out


async def fetch_abbreviation_snapshot(
    pool: asyncpg.Pool, *, tenant_id: UUID, language: str
) -> AbbreviationSnapshot:
    """Read tenant + global rows for ``language`` and freeze them.

    Result is hashed into a stable ``fingerprint`` that participates in
    the idempotence cache key — pipeline_version + this hash invalidate
    the cache when an admin edits the dictionary.
    """
    from db import tenant_connection

    entries: list[AbbreviationEntry] = []
    try:
        async with tenant_connection(pool, tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT tenant_id, expanded, abbreviated, direction,
                       domain, case_sensitive
                FROM abbreviation_dictionary
                WHERE language = $1
                """,
                language,
            )
    except asyncpg.UndefinedTableError:
        logger.warning("abbreviation_dictionary.table_missing")
        return _empty_snapshot()

    for r in rows:
        entries.append(
            AbbreviationEntry(
                expanded=r["expanded"],
                abbreviated=r["abbreviated"],
                direction=r["direction"],
                domain=r["domain"],
                case_sensitive=bool(r["case_sensitive"]),
                is_tenant_override=r["tenant_id"] is not None,
            )
        )
    fingerprint = _fingerprint(entries)
    return AbbreviationSnapshot(entries=tuple(entries), fingerprint=fingerprint)


def _empty_snapshot() -> AbbreviationSnapshot:
    return AbbreviationSnapshot(entries=(), fingerprint=_fingerprint([]))


def _fingerprint(entries: list[AbbreviationEntry]) -> str:
    """Stable hash over the snapshot's content (order-independent)."""
    rows: list[dict[str, Any]] = sorted(
        (
            {
                "expanded": e.expanded,
                "abbreviated": e.abbreviated,
                "direction": e.direction,
                "domain": e.domain,
                "case_sensitive": e.case_sensitive,
                "is_tenant_override": e.is_tenant_override,
            }
            for e in entries
        ),
        key=lambda d: (
            d["expanded"],
            d["abbreviated"],
            d["domain"] or "",
            int(d["is_tenant_override"]),
        ),
    )
    canon = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


async def upsert_tenant_abbreviation(
    pool: asyncpg.Pool,
    *,
    tenant_id: UUID,
    language: str,
    expanded: str,
    abbreviated: str,
    direction: str,
    domain: str | None,
    case_sensitive: bool,
) -> None:
    """Insert or update one tenant row."""
    from db import tenant_connection

    async with tenant_connection(pool, tenant_id) as conn:
        await conn.execute(
            """
            INSERT INTO abbreviation_dictionary
                (tenant_id, language, expanded, abbreviated, direction,
                 domain, case_sensitive)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (tenant_id, language, expanded, abbreviated)
            DO UPDATE SET
                direction = EXCLUDED.direction,
                domain = EXCLUDED.domain,
                case_sensitive = EXCLUDED.case_sensitive,
                updated_at = now()
            """,
            tenant_id,
            language,
            expanded,
            abbreviated,
            direction,
            domain,
            case_sensitive,
        )


async def delete_tenant_abbreviation(
    pool: asyncpg.Pool, *, tenant_id: UUID, abbreviation_id: UUID
) -> bool:
    from db import tenant_connection

    async with tenant_connection(pool, tenant_id) as conn:
        # The RLS policy already restricts to own tenant; the explicit
        # ``tenant_id`` predicate is defence-in-depth.
        result = await conn.execute(
            "DELETE FROM abbreviation_dictionary WHERE id = $1 AND tenant_id = $2",
            abbreviation_id,
            tenant_id,
        )
    return str(result).endswith(" 1")
