"""SQL for the /corpus review surface (sprint 21, ADR-0043/0044).

Runs under tenant_connection as app_role: global candidates are readable via
the candidates_visibility policy; the review WRITE goes through the
corpus_record_review SECURITY DEFINER function (migration 0085) because
app_role deliberately cannot UPDATE global rows.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

import asyncpg


async def list_candidates(
    conn: asyncpg.Connection,
    *,
    review_state: str,
    tier: int | None,
    language: str | None,
    limit: int,
    offset: int,
    sample: str = "oldest",
) -> tuple[list[asyncpg.Record], int]:
    """The review queue: oldest first, so the queue drains in arrival order.
    sample='random' serves the spot-audit sampler — an unbiased draw over the
    whole matching population instead of the fixed oldest slice (offset is
    meaningless there and ignored by callers)."""
    where = ["review_state = $1"]
    args: list[Any] = [review_state]
    if tier is not None:
        args.append(tier)
        where.append(f"tier = ${len(args)}")
    if language is not None:
        args.append(language)
        where.append(f"language = ${len(args)}")
    where_sql = " AND ".join(where)

    total = await conn.fetchval(
        f"SELECT count(*) FROM corpus_candidates WHERE {where_sql}", *args
    )
    order_sql = "random()" if sample == "random" else "created_at ASC"
    args.extend((limit, offset))
    rows = await conn.fetch(
        f"""
        SELECT id, phrase, language, specialty, section_hint, source_kind,
               tier, risk_flags, review_state, review_engine, submitted_by,
               jury_votes, validator_report, created_at
        FROM corpus_candidates
        WHERE {where_sql}
        ORDER BY {order_sql}
        LIMIT ${len(args) - 1} OFFSET ${len(args)}
        """,
        *args,
    )
    return list(rows), int(total or 0)


async def submit_candidate(
    conn: asyncpg.Connection,
    *,
    phrase: str,
    language: str,
    specialty: str | None,
    section_hint: str | None,
    capture: str,
    submitted_by: UUID,
    tenant_id: UUID,
    tier: int,
    risk_flags: Sequence[str],
) -> tuple[UUID, str]:
    """One authored (typed/dictated) phrase → a global candidate row, via the
    corpus_submit_candidate SECURITY DEFINER fn (migrations 0088, 0098) —
    app_role cannot INSERT global rows directly. Returns (id, status) where
    status is 'created' | 'duplicate_candidate' | 'already_in_corpus'.

    `tier` and `risk_flags` come from the shared corpus_risk router in the
    route handler, not from this layer and not from the client: 0088 filed
    every authored phrase as tier 2 with no flags, which kept dose and drug
    phrases out of the tier-3 human queue entirely. The function re-checks the
    pair and refuses the write if the two disagree."""
    row = await conn.fetchrow(
        "SELECT out_id, out_status"
        " FROM corpus_submit_candidate($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        phrase,
        language,
        specialty,
        section_hint,
        capture,
        submitted_by,
        tenant_id,
        tier,
        list(risk_flags),
    )
    assert row is not None  # OUT-param fn always returns one row
    return row["out_id"], row["out_status"]


async def promote_accepted(
    conn: asyncpg.Connection,
    *,
    candidate_ids: list[UUID] | None,
) -> tuple[int, int]:
    """Accepted global candidates → serving corpus, via corpus_promote_accepted
    (migration 0088; mirrors corpus-forge promote_accepted, idempotent).
    Returns (promoted, inserted)."""
    row = await conn.fetchrow(
        "SELECT out_promoted, out_inserted FROM corpus_promote_accepted($1)",
        candidate_ids,
    )
    assert row is not None
    return int(row["out_promoted"] or 0), int(row["out_inserted"] or 0)


async def record_review(
    conn: asyncpg.Connection,
    *,
    candidate_id: UUID,
    reviewer_id: UUID,
    decision: str,
    edited_text: str | None,
    latency_ms: int | None,
    mode: str,
) -> str | None:
    """Returns the new review_state ('audited' for spot-audit rows), or None
    when the candidate is unknown / already decided / not auditable."""
    return await conn.fetchval(
        "SELECT corpus_record_review($1, $2, $3, $4, $5, $6)",
        candidate_id,
        reviewer_id,
        decision,
        edited_text,
        latency_ms,
        mode,
    )


async def candidate_exists(conn: asyncpg.Connection, candidate_id: UUID) -> bool:
    return bool(
        await conn.fetchval("SELECT 1 FROM corpus_candidates WHERE id = $1", candidate_id)
    )


async def stats(conn: asyncpg.Connection) -> dict[str, Any]:
    by_state_rows = await conn.fetch(
        "SELECT review_state, count(*) AS n FROM corpus_candidates GROUP BY review_state"
    )
    by_tier_rows = await conn.fetch(
        "SELECT tier, count(*) AS n FROM corpus_candidates WHERE tier IS NOT NULL GROUP BY tier"
    )
    review_row = await conn.fetchrow(
        """
        SELECT count(*)                                             AS total,
               count(*) FILTER (WHERE decided_at > now() - interval '24 hours')
                                                                    AS last_24h,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms)
                   FILTER (WHERE latency_ms IS NOT NULL)            AS median_latency_ms,
               count(*) FILTER (WHERE mode = 'audit' AND decision = 'reject')
                                                                    AS audit_rejections
        FROM corpus_reviews
        """
    )
    # Quota heatmap: accepted counts per (language, specialty, section, bucket).
    # Bucket is a whitespace-token count (short = ≤5) — a close, cheap proxy
    # for the validator's tokenizer; the CI quota gate stays authoritative.
    quota_rows = await conn.fetch(
        """
        SELECT language,
               COALESCE(specialty, 'general')    AS specialty,
               COALESCE(section_hint, '')        AS section,
               CASE WHEN array_length(regexp_split_to_array(btrim(phrase), '\\s+'), 1) <= 5
                    THEN 'short' ELSE 'long' END AS bucket,
               count(*)                          AS accepted
        FROM autocomplete_phrases
        WHERE source = 'system' AND review_state = 'accepted' AND enabled = TRUE
        GROUP BY 1, 2, 3, 4
        ORDER BY 1, 2, 3, 4
        """
    )
    return {
        "by_state": {str(r["review_state"]): int(r["n"]) for r in by_state_rows},
        "by_tier": {str(r["tier"]): int(r["n"]) for r in by_tier_rows},
        "reviews_total": int(review_row["total"]) if review_row else 0,
        "reviews_last_24h": int(review_row["last_24h"]) if review_row else 0,
        "median_latency_ms": (
            float(review_row["median_latency_ms"])
            if review_row and review_row["median_latency_ms"] is not None
            else None
        ),
        "audit_rejections": int(review_row["audit_rejections"]) if review_row else 0,
        "by_quota_cell": [
            {
                "language": str(r["language"]),
                "specialty": str(r["specialty"]),
                "section": str(r["section"]),
                "bucket": str(r["bucket"]),
                "accepted": int(r["accepted"]),
            }
            for r in quota_rows
        ],
    }


async def telemetry_gaps(
    conn: asyncpg.Connection, *, limit: int
) -> list[asyncpg.Record]:
    """The corpus-forge `gaps` work queue over HTTP: zero-accept scrubbed
    prefixes ranked by volume, via corpus_telemetry_gaps (migration 0090 —
    a deliberate platform-wide aggregate; the fn pins the query to the CLI's
    exact shape and caps the limit)."""
    return list(
        await conn.fetch(
            "SELECT prefix, impressions, accepts, all_timeouts"
            " FROM corpus_telemetry_gaps($1)",
            limit,
        )
    )


async def list_releases(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return list(
        await conn.fetch(
            """
            SELECT version, created_at, phrase_count, manifest_sha256, notes
            FROM corpus_releases
            ORDER BY created_at DESC
            """
        )
    )
