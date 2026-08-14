"""corpus_candidates / corpus_reviews / promote / release SQL.

All candidate writes land in the staging table; nothing reaches
autocomplete_phrases except promote_accepted() (ADR-0043 §3). The promote
is idempotent: the phrases unique index absorbs replays, and candidates
flip to 'promoted' only when their row landed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import asyncpg


@dataclass(frozen=True, slots=True)
class CandidateDraft:
    phrase: str
    dedupe_key: str
    language: str
    source_kind: str
    source_ref: str
    specialty: str | None = None
    section_hint: str | None = None
    generation_batch_id: UUID | None = None
    risk_flags: tuple[str, ...] = ()
    tier: int | None = None
    validator_report: dict[str, Any] = field(default_factory=dict)


async def upsert_candidates(conn: asyncpg.Connection, drafts: list[CandidateDraft]) -> int:
    """Insert new candidates; re-encountering an existing dedupe_key refreshes
    its validator_report (frequency counts move between mining runs) without
    touching review fields. Returns the number of NEW rows."""
    inserted = 0
    for d in drafts:
        row = await conn.fetchrow(
            """
            INSERT INTO corpus_candidates
                (language, specialty, section_hint, phrase, dedupe_key,
                 source_kind, source_ref, generation_batch_id,
                 tier, risk_flags, validator_report)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
            ON CONFLICT (COALESCE(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid),
                         language, dedupe_key)
            DO UPDATE SET validator_report = EXCLUDED.validator_report,
                          updated_at = now()
            RETURNING (xmax = 0) AS is_new
            """,
            d.language,
            d.specialty,
            d.section_hint,
            d.phrase,
            d.dedupe_key,
            d.source_kind,
            d.source_ref,
            d.generation_batch_id,
            d.tier,
            list(d.risk_flags),
            json.dumps(d.validator_report),
        )
        if row is not None and bool(row["is_new"]):
            inserted += 1
    return inserted


async def fetch_accepted_phrases(conn: asyncpg.Connection, *, language: str) -> list[str]:
    """The accepted system corpus — dedupe target and generator avoid-list."""
    rows = await conn.fetch(
        """
        SELECT phrase FROM autocomplete_phrases
        WHERE language = $1 AND source = 'system'
          AND enabled = TRUE AND review_state = 'accepted'
        ORDER BY phrase
        """,
        language,
    )
    return [str(r["phrase"]) for r in rows]


async def record_review(
    conn: asyncpg.Connection,
    *,
    candidate_id: UUID,
    reviewer_id: UUID,
    decision: str,
    edited_text: str | None,
    latency_ms: int | None,
    review_engine: str,
) -> None:
    """One human/jury decision: append the chained review row and move the
    candidate. 'edit' counts as accept-with-text."""
    new_state = "accepted" if decision in ("accept", "edit") else "rejected"
    async with conn.transaction():
        await conn.execute(
            """
            INSERT INTO corpus_reviews
                (candidate_id, reviewer_id, decision, edited_text, latency_ms)
            VALUES ($1, $2, $3, $4, $5)
            """,
            candidate_id,
            reviewer_id,
            decision,
            edited_text,
            latency_ms,
        )
        await conn.execute(
            """
            UPDATE corpus_candidates
            SET review_state = $2,
                phrase = COALESCE($3, phrase),
                dedupe_key = COALESCE($4, dedupe_key),
                reviewed_by = $5,
                reviewed_at = now(),
                review_engine = $6,
                updated_at = now()
            WHERE id = $1 AND review_state = 'candidate'
            """,
            candidate_id,
            new_state,
            edited_text,
            None if edited_text is None else edited_text.lower().strip(),
            reviewer_id,
            review_engine,
        )


async def record_jury_outcome(
    conn: asyncpg.Connection,
    *,
    candidate_id: UUID,
    votes_json: str,
    accepted: bool | None,
    escalate_to_tier: int | None,
    review_engine: str,
) -> None:
    """Jury verdict: accepted=True/False decides; None + escalate_to_tier
    re-routes a split tier-2 vote to tier 3 (human-mandatory)."""
    if accepted is None:
        await conn.execute(
            """
            UPDATE corpus_candidates
            SET jury_votes = $2::jsonb, tier = $3, updated_at = now()
            WHERE id = $1 AND review_state = 'candidate'
            """,
            candidate_id,
            votes_json,
            escalate_to_tier,
        )
        return
    await conn.execute(
        """
        UPDATE corpus_candidates
        SET jury_votes = $2::jsonb,
            review_state = $3,
            reviewed_at = now(),
            review_engine = $4,
            updated_at = now()
        WHERE id = $1 AND review_state = 'candidate'
        """,
        candidate_id,
        votes_json,
        "accepted" if accepted else "rejected",
        review_engine,
    )


async def promote_accepted(conn: asyncpg.Connection) -> tuple[int, int]:
    """Accepted candidates → system-scope autocomplete_phrases rows.

    Idempotent: the phrases unique index absorbs replays via ON CONFLICT
    DO NOTHING, and every accepted candidate flips to 'promoted' whether its
    row was inserted now or already existed. Returns (promoted, inserted)."""
    async with conn.transaction():
        inserted = await conn.fetchval(
            """
            WITH accepted AS (
                SELECT id, phrase, language, specialty, section_hint,
                       source_kind, source_ref, tier, risk_flags,
                       reviewed_by, reviewed_at, review_engine
                FROM corpus_candidates
                WHERE review_state = 'accepted' AND tenant_id IS NULL
            ),
            ins AS (
                INSERT INTO autocomplete_phrases
                    (tenant_id, owner_user_id, phrase, language, specialty,
                     section_hint, source, source_kind, source_ref, tier,
                     review_state, reviewed_by, reviewed_at, review_engine,
                     risk_flags)
                SELECT NULL, NULL, phrase, language, specialty,
                       section_hint, 'system', source_kind, source_ref, tier,
                       'accepted', reviewed_by, reviewed_at, review_engine,
                       risk_flags
                FROM accepted
                ON CONFLICT DO NOTHING
                RETURNING id
            )
            SELECT count(*) FROM ins
            """
        )
        promoted = await conn.fetchval(
            """
            WITH flip AS (
                UPDATE corpus_candidates
                SET review_state = 'promoted', updated_at = now()
                WHERE review_state = 'accepted' AND tenant_id IS NULL
                RETURNING id
            )
            SELECT count(*) FROM flip
            """
        )
    return int(promoted or 0), int(inserted or 0)


async def stamp_release(
    conn: asyncpg.Connection,
    *,
    version: str,
    manifest_sha256: str,
    phrase_count: int,
    published_by: UUID | None,
    notes: str,
) -> None:
    """Publish: register the release and stamp the accepted, not-yet-released
    system rows with its version. One transaction — a release row without its
    stamped phrases (or vice versa) would break bisection."""
    async with conn.transaction():
        await conn.execute(
            """
            INSERT INTO corpus_releases
                (version, manifest_sha256, phrase_count, published_by, notes)
            VALUES ($1, $2, $3, $4, $5)
            """,
            version,
            manifest_sha256,
            phrase_count,
            published_by,
            notes,
        )
        await conn.execute(
            """
            UPDATE autocomplete_phrases
            SET corpus_release = $1, updated_at = now()
            WHERE source = 'system' AND review_state = 'accepted'
              AND corpus_release IS NULL
            """,
            version,
        )


async def apply_release_rows(
    conn: asyncpg.Connection,
    *,
    version: str,
    manifest_sha256: str,
    rows: list[dict[str, Any]],
    notes: str,
) -> tuple[int, bool]:
    """Load a release ARTIFACT into an environment (deployment plan: releases
    are loaded by an idempotent seed job, never carried in migrations).

    Registers the corpus_releases row if absent (a mismatched SHA for an
    existing version raises — releases are immutable), then upserts the
    phrase rows. Returns (rows_inserted, release_row_created)."""
    async with conn.transaction():
        existing_sha = await conn.fetchval(
            "SELECT manifest_sha256 FROM corpus_releases WHERE version = $1", version
        )
        created = False
        if existing_sha is None:
            await conn.execute(
                """
                INSERT INTO corpus_releases (version, manifest_sha256, phrase_count, notes)
                VALUES ($1, $2, $3, $4)
                """,
                version,
                manifest_sha256,
                len(rows),
                notes,
            )
            created = True
        elif str(existing_sha) != manifest_sha256:
            raise RuntimeError(
                f"release {version} already registered with a different manifest "
                f"sha ({existing_sha} != {manifest_sha256}) — releases are immutable"
            )
        inserted = 0
        for row in rows:
            new_id = await conn.fetchval(
                """
                INSERT INTO autocomplete_phrases
                    (tenant_id, owner_user_id, phrase, language, specialty,
                     section_hint, source, source_kind, source_ref, tier,
                     review_state, review_engine, risk_flags, corpus_release)
                VALUES (NULL, NULL, $1, $2, $3, $4, 'system', $5, $6, $7,
                        'accepted', $8, $9, $10)
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                row["phrase"],
                row["language"],
                row["specialty"] or None,
                row["section_hint"] or None,
                row["source_kind"],
                row["source_ref"] or None,
                int(row["tier"]) if row["tier"] else None,
                row["review_engine"] or None,
                [f for f in row["risk_flags"].split(";") if f],
                version,
            )
            if new_id is not None:
                inserted += 1
    return inserted, created


async def retire_release(conn: asyncpg.Connection, *, version: str) -> int:
    """Rollback = retire the release's rows (immutable register stays; the
    rows stop serving via the review_state predicate). Bump the trie cache
    version afterwards — see docs/runbooks/corpus.md."""
    result = await conn.execute(
        """
        UPDATE autocomplete_phrases
        SET review_state = 'retired', updated_at = now()
        WHERE source = 'system' AND corpus_release = $1 AND review_state = 'accepted'
        """,
        version,
    )
    return int(result.split()[-1])


async def fetch_release_rows(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    """The exact row set a release manifest covers: accepted system rows
    (already-released rows keep their stamp; a release is cumulative)."""
    return list(
        await conn.fetch(
            """
            SELECT phrase, language, specialty, section_hint, source_kind,
                   source_ref, tier, review_engine, risk_flags
            FROM autocomplete_phrases
            WHERE source = 'system' AND review_state = 'accepted' AND enabled = TRUE
            ORDER BY language, phrase
            """
        )
    )
