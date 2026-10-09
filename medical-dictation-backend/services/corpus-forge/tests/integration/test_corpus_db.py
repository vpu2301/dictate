"""Sprint-21 DB integration (plan §12): RLS denial on the new tables,
promote-job idempotency, release manifest SHA stability, review-chain
immutability.

Skipped unless ``RUN_DB_INTEGRATION=1``; needs `make dev-up && make migrate-up`.
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest
from corpus_forge.adapters import candidates as cand_db
from corpus_forge.domain.release import ReleaseRow, build_manifest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_INTEGRATION") != "1",
    reason="set RUN_DB_INTEGRATION=1 to run; needs `make dev-up && make migrate-up`",
)

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))
DB_NAME = os.environ.get("POSTGRES_DB", "medical_dictation")
APP_DSN = f"postgresql://app_role:app_role@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
SU_DSN = f"postgresql://postgres:postgres@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"

TENANT_A = "00000000-0000-0000-0000-00000000000a"
TENANT_B = "00000000-0000-0000-0000-00000000000b"


async def _su() -> asyncpg.Connection:
    return await asyncpg.connect(SU_DSN)


async def _cleanup(conn: asyncpg.Connection, marker: str) -> None:
    await conn.execute("SET session_replication_role = replica")
    await conn.execute(
        "DELETE FROM corpus_reviews WHERE candidate_id IN "
        "(SELECT id FROM corpus_candidates WHERE source_ref LIKE $1)",
        f"{marker}%",
    )
    await conn.execute("DELETE FROM autocomplete_phrases WHERE source_ref LIKE $1", f"{marker}%")
    await conn.execute("DELETE FROM corpus_candidates WHERE source_ref LIKE $1", f"{marker}%")
    await conn.execute("SET session_replication_role = DEFAULT")


def _draft(marker: str, phrase: str, **overrides: object) -> cand_db.CandidateDraft:
    base: dict[str, object] = {
        "phrase": phrase,
        "dedupe_key": phrase.lower(),
        "language": "uk",
        "source_kind": "terminology",
        "source_ref": f"{marker}:itest",
        "tier": 2,
    }
    base.update(overrides)
    return cand_db.CandidateDraft(**base)  # type: ignore[arg-type]


class TestCandidatesRLS:
    async def test_app_role_cannot_see_other_tenants_candidates(self) -> None:
        marker = f"itest-rls-{uuid.uuid4().hex[:8]}"
        su = await _su()
        try:
            await su.execute(
                """
                INSERT INTO corpus_candidates
                    (tenant_id, language, phrase, dedupe_key, source_kind, source_ref)
                VALUES ($1, 'uk', 'itest тенант б секретна фраза', $2, 'authored', $3)
                """,
                uuid.UUID(TENANT_B),
                f"itest-b-{marker}",
                f"{marker}:tenant-b",
            )
            app = await asyncpg.connect(APP_DSN)
            try:
                async with app.transaction():
                    await app.execute(
                        "SELECT set_config('app.tenant_id', $1, true)", TENANT_A
                    )
                    rows = await app.fetch(
                        "SELECT phrase FROM corpus_candidates WHERE source_ref LIKE $1",
                        f"{marker}%",
                    )
                assert rows == [], "tenant A saw tenant B's candidate"
            finally:
                await app.close()
        finally:
            await _cleanup(su, marker)
            await su.close()

    async def test_app_role_cannot_insert_global_candidates(self) -> None:
        app = await asyncpg.connect(APP_DSN)
        try:
            async with app.transaction():
                await app.execute("SELECT set_config('app.tenant_id', $1, true)", TENANT_A)
                with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                    await app.execute(
                        """
                        INSERT INTO corpus_candidates
                            (tenant_id, language, phrase, dedupe_key, source_kind, source_ref)
                        VALUES (NULL, 'uk', 'itest глобальна спроба', 'itest глобальна спроба',
                                'authored', 'itest:global-attempt')
                        """
                    )
        finally:
            await app.close()


class TestReviewChain:
    async def test_reviews_are_immutable_and_chained(self) -> None:
        marker = f"itest-chain-{uuid.uuid4().hex[:8]}"
        su = await _su()
        try:
            cand_id = await su.fetchval(
                """
                INSERT INTO corpus_candidates
                    (language, phrase, dedupe_key, source_kind, source_ref)
                VALUES ('uk', 'itest фраза для ланцюга', $1, 'terminology', $2)
                RETURNING id
                """,
                f"chain-{marker}",
                f"{marker}:chain",
            )
            reviewer = uuid.uuid4()
            for decision in ("accept", "reject"):
                await su.execute(
                    "INSERT INTO corpus_reviews (candidate_id, reviewer_id, decision) VALUES ($1, $2, $3)",
                    cand_id,
                    reviewer,
                    decision,
                )
            linked = await su.fetchval(
                """
                SELECT r2.prev_hash = r1.row_hash
                FROM corpus_reviews r1
                JOIN corpus_reviews r2 ON r2.seq = r1.seq + 1
                WHERE r1.candidate_id = $1 AND r2.candidate_id = $1
                """,
                cand_id,
            )
            assert linked is True
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await su.execute(
                    "UPDATE corpus_reviews SET decision = 'accept' WHERE candidate_id = $1", cand_id
                )
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await su.execute("DELETE FROM corpus_reviews WHERE candidate_id = $1", cand_id)
        finally:
            await _cleanup(su, marker)
            await su.close()


class TestPromoteIdempotency:
    async def test_double_promote_inserts_once(self) -> None:
        marker = f"itest-promote-{uuid.uuid4().hex[:8]}"
        su = await _su()
        try:
            await cand_db.upsert_candidates(
                su, [_draft(marker, f"itest промоут фраза {marker}")]
            )
            await su.execute(
                "UPDATE corpus_candidates SET review_state = 'accepted', review_engine = 'human' "
                "WHERE source_ref LIKE $1",
                f"{marker}%",
            )
            promoted1, inserted1 = await cand_db.promote_accepted(su)
            assert inserted1 >= 1
            promoted2, inserted2 = await cand_db.promote_accepted(su)
            assert inserted2 == 0, "second promote must not duplicate rows"

            count = await su.fetchval(
                "SELECT count(*) FROM autocomplete_phrases WHERE source_ref LIKE $1", f"{marker}%"
            )
            assert count == 1
            state = await su.fetchval(
                "SELECT review_state FROM corpus_candidates WHERE source_ref LIKE $1", f"{marker}%"
            )
            assert state == "promoted"
        finally:
            await _cleanup(su, marker)
            await su.close()

    async def test_promoted_row_is_served_and_candidate_rows_are_not(self) -> None:
        """The one serving predicate: only review_state='accepted' reaches
        the trie feed."""
        marker = f"itest-serve-{uuid.uuid4().hex[:8]}"
        su = await _su()
        try:
            await su.execute(
                """
                INSERT INTO autocomplete_phrases
                    (phrase, language, source, source_kind, source_ref, review_state)
                VALUES ($1, 'uk', 'system', 'mined', $2, 'candidate')
                """,
                f"itest незатверджена фраза {marker}",
                f"{marker}:unaccepted",
            )
            served = await su.fetch(
                """
                SELECT phrase FROM autocomplete_phrases
                WHERE language = 'uk' AND enabled = TRUE AND review_state = 'accepted'
                  AND source_ref LIKE $1
                """,
                f"{marker}%",
            )
            assert served == []
        finally:
            await _cleanup(su, marker)
            await su.close()


class TestRecordReviewFn:
    async def test_app_role_reviews_global_candidate_via_definer_fn(self) -> None:
        """The HTTP review path: app_role can't UPDATE global rows directly
        (RLS), but corpus_record_review (0085, SECURITY DEFINER) can."""
        marker = f"itest-fn-{uuid.uuid4().hex[:8]}"
        su = await _su()
        try:
            cand_id = await su.fetchval(
                """
                INSERT INTO corpus_candidates
                    (language, phrase, dedupe_key, source_kind, source_ref, tier)
                VALUES ('uk', 'itest фраза для definer', $1, 'terminology', $2, 2)
                RETURNING id
                """,
                f"fn-{marker}",
                f"{marker}:fn",
            )
            app = await asyncpg.connect(APP_DSN)
            try:
                async with app.transaction():
                    await app.execute("SELECT set_config('app.tenant_id', $1, true)", TENANT_A)
                    new_state = await app.fetchval(
                        "SELECT corpus_record_review($1, $2, 'accept', NULL, 9000, 'review')",
                        cand_id,
                        uuid.uuid4(),
                    )
                    assert new_state == "accepted"
                    # second decision → NULL (already decided; HTTP maps to 409)
                    again = await app.fetchval(
                        "SELECT corpus_record_review($1, $2, 'reject', NULL, 1000, 'review')",
                        cand_id,
                        uuid.uuid4(),
                    )
                    assert again is None
            finally:
                await app.close()
            mode = await su.fetchval(
                "SELECT mode FROM corpus_reviews WHERE candidate_id = $1", cand_id
            )
            assert mode == "review"
        finally:
            await _cleanup(su, marker)
            await su.close()

    async def test_audit_mode_records_without_state_change(self) -> None:
        marker = f"itest-audit-{uuid.uuid4().hex[:8]}"
        su = await _su()
        try:
            cand_id = await su.fetchval(
                """
                INSERT INTO corpus_candidates
                    (language, phrase, dedupe_key, source_kind, source_ref, tier,
                     review_state, review_engine)
                VALUES ('uk', 'itest фраза для аудиту', $1, 'generated', $2, 2,
                        'accepted', 'jury:gemma3:1b:v1')
                RETURNING id
                """,
                f"audit-{marker}",
                f"{marker}:audit",
            )
            outcome = await su.fetchval(
                "SELECT corpus_record_review($1, $2, 'reject', NULL, 5000, 'audit')",
                cand_id,
                uuid.uuid4(),
            )
            assert outcome == "audited"
            state = await su.fetchval(
                "SELECT review_state FROM corpus_candidates WHERE id = $1", cand_id
            )
            assert state == "accepted", "spot-audit must not change candidate state"
            # human-decided (non-jury) rows are not auditable
            human_id = await su.fetchval(
                """
                INSERT INTO corpus_candidates
                    (language, phrase, dedupe_key, source_kind, source_ref,
                     review_state, review_engine)
                VALUES ('uk', 'itest людське рішення', $1, 'terminology', $2,
                        'accepted', 'human')
                RETURNING id
                """,
                f"human-{marker}",
                f"{marker}:human",
            )
            refused = await su.fetchval(
                "SELECT corpus_record_review($1, $2, 'reject', NULL, 5000, 'audit')",
                human_id,
                uuid.uuid4(),
            )
            assert refused is None
        finally:
            await _cleanup(su, marker)
            await su.close()


class TestReleaseManifest:
    async def test_manifest_sha_stable_across_runs(self) -> None:
        su = await _su()
        try:
            records = await cand_db.fetch_release_rows(su)
        finally:
            await su.close()
        rows = [
            ReleaseRow(
                phrase=str(r["phrase"]),
                language=str(r["language"]),
                specialty=r["specialty"],
                section_hint=r["section_hint"],
                source_kind=str(r["source_kind"]),
                source_ref=r["source_ref"],
                tier=r["tier"],
                review_engine=r["review_engine"],
                risk_flags=tuple(r["risk_flags"] or ()),
            )
            for r in records
        ]
        _, _, sha1 = build_manifest(version="v9.9.9", rows=rows, fluency_filter="heuristic-v1")
        _, _, sha2 = build_manifest(version="v9.9.9", rows=rows, fluency_filter="heuristic-v1")
        assert sha1 == sha2
