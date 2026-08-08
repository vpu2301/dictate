"""S03 verification protocol §9: live checks against the running stack.

Env-gated: RUN_RETRIEVAL_LIVE=1 with the platform stack, opensearch, the
model gateway (:8015) and evidence-retrieval (:8011) all up, fixture corpus
ingested (S02) and snapshots fixtures-v1/v2 frozen.
"""

from __future__ import annotations

import os

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_RETRIEVAL_LIVE") != "1",
    reason="live retrieval tests are env-gated (RUN_RETRIEVAL_LIVE=1)",
)

BASE = os.environ.get("EVA_RETRIEVAL_URL", "http://127.0.0.1:8011")
TENANT_A = "00000000-0000-0000-0000-00000000000a"


def _post(payload: dict[str, object]) -> httpx.Response:
    return httpx.post(f"{BASE}/retrieve", json=payload, timeout=120)


class TestDeterminism:
    def test_repeated_calls_identical(self) -> None:
        """§9.2 determinism ×100: identical requests ⇒ identical passage
        lists (the first call computes, the rest hit the deterministic cache;
        both paths must agree byte-for-byte on ids and scores)."""
        payload = {
            "query": "metformin first line therapy",
            "k": 10,
            "sources": ["local_corpus"],
            "tenant_id": TENANT_A,
        }
        first = _post(payload)
        assert first.status_code == 200
        reference = [(p["chunk_id"], p["scores"]["final"]) for p in first.json()["passages"]]
        assert reference, "expected non-empty results for the fixture corpus"
        for _ in range(99):
            again = _post(payload)
            assert again.status_code == 200
            assert [
                (p["chunk_id"], p["scores"]["final"]) for p in again.json()["passages"]
            ] == reference


class TestFilters:
    def test_authority_filter_both_engines_agree(self) -> None:
        """§9.1 filter matrix: fixtures are all international ⇒ 'national'
        filter empties BOTH engines (no half-filtered fusion leaks)."""
        base = {"query": "diabetes", "k": 10, "sources": ["local_corpus"], "tenant_id": TENANT_A}
        national = _post({**base, "filters": {"authority": ["national"]}})
        assert national.status_code == 200
        assert national.json()["passages"] == []
        international = _post({**base, "filters": {"authority": ["international"]}})
        assert international.status_code == 200
        assert international.json()["passages"]

    def test_date_filter(self) -> None:
        base = {
            "query": "diabetes recommendations",
            "k": 20,
            "sources": ["local_corpus"],
            "tenant_id": TENANT_A,
        }
        dated = _post({**base, "filters": {"date_from": "2024-01-01"}})
        assert dated.status_code == 200
        for p in dated.json()["passages"]:
            assert p["published_at"] is not None
            assert p["published_at"] >= "2024-01-01"

    def test_k_cap_negative(self) -> None:
        response = _post({"query": "x", "k": 51, "tenant_id": TENANT_A})
        assert response.status_code == 422

    def test_unknown_source_rejected(self) -> None:
        response = _post({"query": "x", "sources": ["wikipedia"], "tenant_id": TENANT_A})
        assert response.status_code == 422


class TestSnapshotPinning:
    def _snapshot_id(self, label: str) -> str:
        import asyncio

        import asyncpg

        async def fetch() -> str:
            conn = await asyncpg.connect(
                os.environ.get(
                    "EVIDENCE_TEST_ADMIN_DSN",
                    "postgresql://postgres:postgres@localhost:5432/medical_dictation",
                )
            )
            try:
                return str(
                    await conn.fetchval("SELECT id FROM corpus_snapshots WHERE label = $1", label)
                )
            finally:
                await conn.close()

        return asyncio.run(fetch())

    def test_old_snapshot_returns_pre_update_docset(self) -> None:
        """§9.3: «Третя версія» exists only in version 3 of the МОЗ protocol,
        ingested AFTER fixtures-v2 was frozen; pinned retrieval must not see
        it, unpinned must."""
        base = {
            "query": "Додаток В третя версія маркер",
            "k": 20,
            "sources": ["local_corpus"],
            "tenant_id": TENANT_A,
        }
        unpinned = _post(base)
        assert unpinned.status_code == 200
        texts_unpinned = " ".join(p["text"] for p in unpinned.json()["passages"])
        assert "Третя версія" in texts_unpinned

        pinned = _post({**base, "snapshot_id": self._snapshot_id("fixtures-v2")})
        assert pinned.status_code == 200
        texts_pinned = " ".join(p["text"] for p in pinned.json()["passages"])
        assert "Третя версія" not in texts_pinned


class TestStubsHonesty:
    def test_stub_sources_report_unavailable(self) -> None:
        response = _post(
            {
                "query": "aspirin",
                "sources": ["local_corpus", "pubmed", "web"],
                "tenant_id": TENANT_A,
            }
        )
        assert response.status_code == 200
        body = response.json()
        by_kind = {m["kind"]: m for m in body["connector_meta"]}
        assert by_kind["pubmed"]["status"] == "unavailable"
        assert by_kind["web"]["status"] == "unavailable"
        assert by_kind["pubmed"]["count"] == 0
        # honest degradation: a requested-but-unavailable source flags it
        assert body["degraded"] is True
