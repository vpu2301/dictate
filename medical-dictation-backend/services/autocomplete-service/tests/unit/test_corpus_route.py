"""Sprint-21 /corpus review surface — handlers exercised with a fake pool
(test_phrases_list_route.py style); RLS + the SECURITY DEFINER path are
integration-tested in corpus-forge."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from autocomplete_service.deps import install_state
from autocomplete_service.routers.corpus import (
    CandidateListDTO,
    PromoteRequest,
    ReviewRequest,
    SubmitCandidateRequest,
    corpus_gaps,
    corpus_releases,
    corpus_stats,
    list_candidates,
    promote_candidates,
    review_candidate,
    submit_candidate,
)
from fastapi import HTTPException
from pydantic import ValidationError

from auth import Claims

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=UTC)


def _claims(roles=("clinician",)) -> Claims:
    return Claims(
        sub=uuid4(), tid=uuid4(), roles=list(roles), scope="openid",
        sid="s", iss="test", aud="mdx-api", exp=2_000_000_000, iat=1,
    )


class _FakeTx:
    async def start(self) -> None: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...


class _FakeConn:
    def __init__(self, *, rows=None, scalars=None, row=None) -> None:
        self.rows = rows or []
        self.scalars = list(scalars or [])
        self.row = row
        self.executed: list[tuple[str, tuple]] = []
        self.fetches: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args) -> None:
        self.executed.append((sql, args))

    async def fetch(self, sql: str, *args) -> list[dict]:
        self.fetches.append((sql, args))
        return self.rows

    async def fetchval(self, sql: str, *args):
        self.fetches.append((sql, args))
        return self.scalars.pop(0) if self.scalars else None

    async def fetchrow(self, sql: str, *args):
        self.fetches.append((sql, args))
        return self.row

    def transaction(self) -> _FakeTx:
        return _FakeTx()


class _Acquire:
    def __init__(self, conn) -> None:
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakePool:
    def __init__(self, conn) -> None:
        self._conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


class _FakeAudit:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def write_event(self, **kw):
        self.events.append(kw)


class _FakeRateLimiter:
    def __init__(self, allowed=True) -> None:
        self.allowed = allowed

    async def check(self, *, user_id):
        return (self.allowed, 0 if self.allowed else 30)


class _FakeMetric:
    def add(self, *a, **kw) -> None: ...


class _FakeTrieCache:
    def __init__(self) -> None:
        self.bumped: list = []

    async def bump_version_tag(self, *, tenant_id) -> None:
        self.bumped.append(tenant_id)


class _State:
    def __init__(self, conn) -> None:
        self.app_pool = _FakePool(conn)
        self.audit_writer = _FakeAudit()
        self.phrase_rate_limiter = _FakeRateLimiter()
        self.pii_rejections_metric = _FakeMetric()
        self.trie_cache = _FakeTrieCache()


def _candidate_row(**over) -> dict:
    row = {
        "id": uuid4(),
        "phrase": "прийом бісопрололу п’ять міліграм",
        "language": "uk",
        "specialty": "cardiology",
        "section_hint": "plan",
        "source_kind": "terminology",
        "tier": 3,
        "risk_flags": ["dose", "drug"],
        "review_state": "candidate",
        "review_engine": None,
        "submitted_by": None,
        "jury_votes": "[]",
        "validator_report": "{}",
        "created_at": NOW,
    }
    row.update(over)
    return row


async def test_list_candidates_maps_rows_and_decodes_json():
    conn = _FakeConn(rows=[_candidate_row()], scalars=[1])
    install_state(_State(conn))

    out = await list_candidates(
        _claims(), review_state="candidate", tier=3, language="uk", limit=50, offset=0
    )

    assert isinstance(out, CandidateListDTO)
    assert out.total == 1
    item = out.items[0]
    assert item.risk_flags == ["dose", "drug"]
    assert item.jury_votes == [] and item.validator_report == {}
    # tier filter is bound in the SQL
    count_sql, count_args = conn.fetches[0]
    assert "tier = $2" in count_sql and count_args[1] == 3


async def test_list_candidates_random_sample_changes_order():
    conn = _FakeConn(rows=[], scalars=[0])
    install_state(_State(conn))

    await list_candidates(
        _claims(), review_state="accepted", tier=None, language=None,
        limit=25, offset=0, sample="random",
    )
    list_sql, _ = conn.fetches[1]
    assert "ORDER BY random()" in list_sql


async def test_submit_creates_candidate_and_audits():
    cid = uuid4()
    conn = _FakeConn(row={"out_id": cid, "out_status": "created"})
    state = _State(conn)
    install_state(state)
    claims = _claims()

    out = await submit_candidate(
        SubmitCandidateRequest(
            phrase="огляд без особливостей", language="uk",
            specialty="cardiology", capture="dictated",
        ),
        claims,
    )

    assert out.id == cid and out.review_state == "candidate"
    sql, args = conn.fetches[0]
    assert "corpus_submit_candidate" in sql
    assert args[4] == "dictated" and args[5] == claims.sub and args[6] == claims.tid
    event = state.audit_writer.events[0]
    assert event["kind"] == "corpus.candidate_submitted"
    # PHI hygiene: never the phrase text, only its length
    assert "phrase" not in event["payload"]
    assert event["payload"]["capture"] == "dictated"
    assert event["payload"]["text_length"] == len("огляд без особливостей")


# ── tier routing (migration 0098) ──────────────────────────────────────
#
# Before 0098 this route wrote `tier = 2, risk_flags = '{}'` for every phrase
# it was handed. The review queue serves tier 3, so nothing authored in the
# console could ever reach a reviewer — and a phrase carrying a dose was one
# jury majority away from a clinician's cursor with no human having read it.
# These assert the routing is the shared corpus_risk one, on both sides of it:
# what goes to the database, and what comes back to the author.


async def test_submit_routes_risk_flagged_phrase_to_the_human_queue():
    conn = _FakeConn(row={"out_id": uuid4(), "out_status": "created"})
    state = _State(conn)
    install_state(state)

    # "без" is a negation; the dose regex catches the digits and the unit.
    out = await submit_candidate(
        SubmitCandidateRequest(phrase="бісопролол 5 мг без пауз", language="uk"),
        _claims(),
    )

    assert out.tier == 3
    assert "dose" in out.risk_flags and "negation" in out.risk_flags
    _, args = conn.fetches[0]
    assert args[7] == 3                       # p_tier
    assert set(args[8]) == set(out.risk_flags)  # p_risk_flags
    # The audit trail records the routing decision, not just the arrival.
    assert state.audit_writer.events[0]["payload"]["tier"] == 3


async def test_submit_routes_clean_phrase_to_tier_2():
    conn = _FakeConn(row={"out_id": uuid4(), "out_status": "created"})
    install_state(_State(conn))

    out = await submit_candidate(
        SubmitCandidateRequest(phrase="загальний стан задовільний", language="uk"),
        _claims(),
    )

    assert out.tier == 2 and out.risk_flags == []
    _, args = conn.fetches[0]
    assert args[7] == 2 and args[8] == []


@pytest.mark.parametrize(
    ("fn_status", "error_code"),
    [
        ("duplicate_candidate", "candidate_already_exists"),
        ("already_in_corpus", "phrase_already_in_corpus"),
    ],
)
async def test_submit_duplicate_maps_409(fn_status, error_code):
    existing = uuid4()
    conn = _FakeConn(row={"out_id": existing, "out_status": fn_status})
    state = _State(conn)
    install_state(state)

    with pytest.raises(HTTPException) as exc:
        await submit_candidate(
            SubmitCandidateRequest(phrase="скарги відсутні", language="uk"),
            _claims(),
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == {"error": error_code, "id": str(existing)}
    assert state.audit_writer.events == []  # nothing entered the queue


async def test_submit_rejects_pii_before_any_write():
    conn = _FakeConn()
    state = _State(conn)
    install_state(state)

    with pytest.raises(HTTPException) as exc:
        await submit_candidate(
            SubmitCandidateRequest(phrase="тел +380671234567", language="uk"),
            _claims(),
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["error"] == "pii_detected"
    assert conn.fetches == []  # rejected at the boundary — no SQL ran
    # the SEC audit event fired, and it carries length, never text
    event = state.audit_writer.events[0]
    assert event["kind"] == "autocomplete.phrase.write_rejected_pii"
    assert "phrase" not in event["payload"]


async def test_submit_rate_limited_429():
    conn = _FakeConn()
    state = _State(conn)
    state.phrase_rate_limiter = _FakeRateLimiter(allowed=False)
    install_state(state)

    with pytest.raises(HTTPException) as exc:
        await submit_candidate(
            SubmitCandidateRequest(phrase="без динаміки", language="uk"), _claims()
        )
    assert exc.value.status_code == 429


async def test_promote_maps_counts_audits_and_bumps_cache():
    conn = _FakeConn(row={"out_promoted": 3, "out_inserted": 2})
    state = _State(conn)
    install_state(state)
    claims = _claims(roles=("tenant_admin",))
    ids = [uuid4(), uuid4(), uuid4()]

    out = await promote_candidates(PromoteRequest(candidate_ids=ids), claims)

    assert out.promoted == 3 and out.inserted == 2
    sql, args = conn.fetches[0]
    assert "corpus_promote_accepted" in sql and args[0] == ids
    event = state.audit_writer.events[0]
    assert event["kind"] == "corpus.candidates_promoted"
    assert event["payload"] == {"promoted": 3, "inserted": 2, "requested_ids": 3}
    assert state.trie_cache.bumped == [claims.tid]


async def test_promote_nothing_accepted_is_quiet():
    conn = _FakeConn(row={"out_promoted": 0, "out_inserted": 0})
    state = _State(conn)
    install_state(state)

    out = await promote_candidates(PromoteRequest(), _claims(roles=("tenant_admin",)))

    assert out.promoted == 0 and out.inserted == 0
    _, args = conn.fetches[0]
    assert args[0] is None  # promote-all form
    assert state.audit_writer.events == []
    assert state.trie_cache.bumped == []


async def test_review_records_decision_and_audits():
    conn = _FakeConn(scalars=["accepted"])
    state = _State(conn)
    install_state(state)
    cid = uuid4()

    out = await review_candidate(
        cid,
        ReviewRequest(decision="accept", latency_ms=12_000),
        _claims(),
    )

    assert out.review_state == "accepted"
    sql, args = conn.fetches[0]
    assert "corpus_record_review" in sql
    assert args[2:] == ("accept", None, 12_000, "review")
    event = state.audit_writer.events[0]
    assert event["kind"] == "corpus.candidate_reviewed"
    # PHI hygiene: the audit payload never carries the phrase text
    assert "phrase" not in event["payload"]
    assert event["payload"]["latency_ms"] == 12_000


async def test_review_conflict_when_already_decided():
    # fn returns NULL but the candidate exists → 409
    conn = _FakeConn(scalars=[None, 1])
    install_state(_State(conn))

    with pytest.raises(HTTPException) as exc:
        await review_candidate(uuid4(), ReviewRequest(decision="reject"), _claims())
    assert exc.value.status_code == 409


async def test_review_404_when_unknown():
    conn = _FakeConn(scalars=[None, None])
    install_state(_State(conn))

    with pytest.raises(HTTPException) as exc:
        await review_candidate(uuid4(), ReviewRequest(decision="accept"), _claims())
    assert exc.value.status_code == 404


async def test_audit_mode_passes_through_and_maps_conflict_detail():
    conn = _FakeConn(scalars=["audited"])
    install_state(_State(conn))

    out = await review_candidate(
        uuid4(), ReviewRequest(decision="reject", mode="audit"), _claims()
    )
    assert out.review_state == "audited"
    _, args = conn.fetches[0]
    assert args[-1] == "audit"


def test_review_request_validation_is_strict():
    with pytest.raises(ValidationError):
        ReviewRequest(decision="edit")  # edit needs text
    with pytest.raises(ValidationError):
        ReviewRequest(decision="edit", edited_text="x", mode="audit")  # audit can't edit
    with pytest.raises(ValidationError):
        ReviewRequest(decision="accept", extra_field=1)  # extra=forbid


async def test_stats_shapes_payload():
    conn = _FakeConn(
        rows=[],
        scalars=[],
    )

    async def fake_fetch(sql, *args):
        conn.fetches.append((sql, args))
        if "GROUP BY review_state" in sql:
            return [{"review_state": "candidate", "n": 5}]
        if "GROUP BY tier" in sql:
            return [{"tier": 3, "n": 2}]
        return [
            {"language": "uk", "specialty": "cardiology", "section": "plan",
             "bucket": "short", "accepted": 4}
        ]

    async def fake_fetchrow(sql, *args):
        return {"total": 7, "last_24h": 3, "median_latency_ms": 11_500.0, "audit_rejections": 1}

    conn.fetch = fake_fetch
    conn.fetchrow = fake_fetchrow
    install_state(_State(conn))

    out = await corpus_stats(_claims())
    assert out.by_state == {"candidate": 5}
    assert out.by_tier == {"3": 2}
    assert out.reviews_last_24h == 3
    assert out.audit_rejections == 1
    assert out.by_quota_cell[0]["bucket"] == "short"


async def test_releases_lists_register():
    conn = _FakeConn(
        rows=[
            {"version": "v0.0.1", "created_at": NOW, "phrase_count": 32,
             "manifest_sha256": "a" * 64, "notes": "smoke"}
        ]
    )
    install_state(_State(conn))

    out = await corpus_releases(_claims())
    assert out.items[0].version == "v0.0.1"
    assert out.items[0].phrase_count == 32


async def test_gaps_maps_rows_and_passes_limit():
    conn = _FakeConn(
        rows=[
            {"prefix": "скарги на біль у", "impressions": 41, "accepts": 0,
             "all_timeouts": False},
            {"prefix": "діагноз гіпертонічна", "impressions": 12, "accepts": 0,
             "all_timeouts": True},
        ]
    )
    install_state(_State(conn))

    out = await corpus_gaps(_claims(), limit=25)

    assert [g.prefix for g in out.items] == [
        "скарги на біль у", "діагноз гіпертонічна",
    ]
    assert out.items[0].impressions == 41
    assert out.items[1].all_timeouts is True
    sql, args = conn.fetches[0]
    assert "corpus_telemetry_gaps" in sql
    assert args == (25,)


async def test_gaps_empty_is_an_empty_list():
    conn = _FakeConn(rows=[])
    install_state(_State(conn))

    out = await corpus_gaps(_claims())
    assert out.items == []
