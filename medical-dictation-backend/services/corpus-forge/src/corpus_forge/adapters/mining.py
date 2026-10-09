"""Report n-gram miner + telemetry gap miner (sprint plan §2/§2.1).

MINING_QUERY below is the exact text the DPO signs before first execution
(docs/signoffs/sprint-21-dpo.md). Editing it invalidates the sign-off —
the CLI records sha256(MINING_QUERY) in every `corpus.mining_run` audit
event so drift is detectable.

Hard gates, enforced in SQL, not Python (ADR-0043 §7):
  * n-gram length 3-12 tokens, ≤80 chars;
  * k-anonymity — ≥ $2 distinct authors across ≥ $3 tenants;
  * frequency floor $4;
  * no n-gram containing a token from any patient's name.
Python re-checks k-anonymity (defense in depth) and drops PII matches
entirely (never redacts) before a candidate row is written.

Mining is cross-tenant by construction, so it runs on a dedicated
read-only DSN, not through tenant_connection() — see docs/runbooks/corpus.md.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import asyncpg

MINING_QUERY = """
WITH finalized AS (
    -- Current version of every finalized/signed report; drafts never mined.
    SELECT rv.rendered_text,
           rv.created_by,
           r.tenant_id,
           rv.id AS version_id
    FROM reports r
    JOIN report_versions rv ON rv.id = r.current_version_id
    WHERE r.status IN ('finalized', 'signed')
),
docs AS (
    SELECT version_id,
           tenant_id,
           created_by,
           array_remove(
               regexp_split_to_array(lower(rendered_text), '[^[:alnum:]’''-]+'),
               ''
           ) AS toks
    FROM finalized
),
grams AS (
    SELECT d.tenant_id,
           d.created_by,
           array_to_string(d.toks[s : s + n - 1], ' ') AS gram
    FROM docs d
    CROSS JOIN generate_series(3, 12) AS n
    CROSS JOIN LATERAL generate_series(1, cardinality(d.toks) - n + 1) AS s
),
name_tokens AS (
    -- Every token of every patient name, either language. Cheap, catches
    -- what regex misses.
    SELECT DISTINCT tok
    FROM patients p,
         LATERAL unnest(
             regexp_split_to_array(lower(p.name_uk || ' ' || COALESCE(p.name_en, '')),
                                   '[^[:alnum:]’''-]+')
         ) AS tok
    WHERE tok <> ''
)
SELECT gram,
       count(*)                    AS frequency,
       count(DISTINCT created_by)  AS distinct_authors,
       count(DISTINCT tenant_id)   AS distinct_tenants
FROM grams
WHERE char_length(gram) <= 80
  AND NOT EXISTS (
      SELECT 1
      FROM unnest(string_to_array(gram, ' ')) AS gt(tok)
      WHERE gt.tok IN (SELECT tok FROM name_tokens)
  )
GROUP BY gram
HAVING count(DISTINCT created_by) >= $1
   AND count(DISTINCT tenant_id)  >= $2
   AND count(*)                   >= $3
ORDER BY count(*) DESC
LIMIT $4
"""

GAPS_QUERY = """
SELECT prefix_scrubbed,
       count(*) AS impressions,
       count(*) FILTER (WHERE event_type = 'accepted') AS accepts,
       bool_and(event_type = 'timeout')                AS all_timeouts
FROM autocomplete_telemetry
WHERE source = 'autocomplete'
  AND prefix_scrubbed <> ''
  AND position('<redacted_PII>' IN prefix_scrubbed) = 0
GROUP BY prefix_scrubbed
HAVING count(*) FILTER (WHERE event_type = 'accepted') = 0
ORDER BY count(*) DESC
LIMIT $1
"""


def mining_query_sha256() -> str:
    return hashlib.sha256(MINING_QUERY.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MinedGram:
    gram: str
    frequency: int
    distinct_authors: int
    distinct_tenants: int


@dataclass(frozen=True, slots=True)
class GapPrefix:
    prefix: str
    impressions: int
    accepts: int
    all_timeouts: bool


async def mine_ngrams(
    conn: asyncpg.Connection,
    *,
    min_authors: int,
    min_tenants: int,
    min_frequency: int,
    limit: int = 20_000,
) -> list[MinedGram]:
    rows = await conn.fetch(MINING_QUERY, min_authors, min_tenants, min_frequency, limit)
    return [
        MinedGram(
            gram=r["gram"],
            frequency=int(r["frequency"]),
            distinct_authors=int(r["distinct_authors"]),
            distinct_tenants=int(r["distinct_tenants"]),
        )
        for r in rows
    ]


async def telemetry_gaps(conn: asyncpg.Connection, *, limit: int = 5_000) -> list[GapPrefix]:
    rows = await conn.fetch(GAPS_QUERY, limit)
    return [
        GapPrefix(
            prefix=r["prefix_scrubbed"],
            impressions=int(r["impressions"]),
            accepts=int(r["accepts"]),
            all_timeouts=bool(r["all_timeouts"]),
        )
        for r in rows
    ]
