"""Ranking unit tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from autocomplete_service.ranking import (
    PhraseRecord,
    bayesian_acceptance,
    confidence,
    diversity_filter,
    length_score,
    recency_boost,
    score,
)


def _rec(**overrides) -> PhraseRecord:
    base = {
        "id": "a",
        "phrase": "хворий поступив зі скаргами",
        "source": "system",
        "impression_count": 0,
        "acceptance_count": 0,
        "last_accepted_at": None,
    }
    base.update(overrides)
    return PhraseRecord(**base)


def test_bayesian_prior_zero_starts_low_nonzero():
    assert bayesian_acceptance(0, 0) == 1 / 10
    assert 0 < bayesian_acceptance(0, 0) < 0.2


def test_bayesian_grows_with_accepts():
    a = bayesian_acceptance(0, 0)
    b = bayesian_acceptance(100, 50)
    assert b > a
    assert b > 0.4


def test_recency_boost_recent_lifts():
    today = datetime.now(UTC)
    assert recency_boost(today, now=today) > 1.0
    week_ago = today - timedelta(days=7)
    assert recency_boost(week_ago, now=today) > 1.0
    assert recency_boost(week_ago, now=today) < recency_boost(today, now=today)


def test_recency_boost_old_neutral():
    today = datetime.now(UTC)
    old = today - timedelta(days=60)
    assert recency_boost(old, now=today) == 1.0
    assert recency_boost(None) == 1.0


def test_recency_boost_naive_datetime_degrades_to_no_boost():
    """asyncpg decodes '±infinity'::timestamptz as a naive datetime
    (datetime.min/max); ranking must return neutral 1.0, not raise
    TypeError against the aware now() (S10 /suggest 500 fix)."""
    assert recency_boost(datetime.min) == 1.0
    assert recency_boost(datetime.max) == 1.0
    assert recency_boost(datetime(2026, 7, 12, 12, 0)) == 1.0  # any naive value
    # ...and via the full score path with a poisoned record:
    assert score(_rec(last_accepted_at=datetime.min)) > 0.0


def test_length_score_prefers_shorter():
    assert length_score("a" * 10) > length_score("a" * 50)
    assert length_score("a" * 80) < length_score("a" * 10)


def test_source_priority_user_over_tenant_over_system():
    u = score(_rec(source="user"))
    t = score(_rec(source="tenant"))
    s = score(_rec(source="system"))
    assert u > t > s


def test_diversity_filter_drops_near_duplicates():
    a = _rec(id="a", phrase="alpha-beta-gamma")
    b = _rec(id="b", phrase="alpha-beta-gamna")  # 1 char off
    c = _rec(id="c", phrase="totally different")
    ranked = [
        (a, 0.9, "alpha-beta-gamma"),
        (b, 0.8, "alpha-beta-gamna"),
        (c, 0.7, "totally different"),
    ]
    kept = diversity_filter(ranked, levenshtein_threshold=3)
    assert len(kept) == 2
    assert kept[0][0].id == "a"
    assert kept[1][0].id == "c"


def test_confidence_in_unit_interval():
    assert 0.0 <= confidence(0.0) <= 1.0
    assert 0.0 <= confidence(1.0) <= 1.0
    assert confidence(1.0) > confidence(0.0)


def test_determinism_shuffled_input_identical_output():
    """Response stability for identical corpus+counters is an API property:
    equal-score candidates are tie-broken by text, so any input order
    yields the same ranked output."""
    import random

    from autocomplete_service.suggest import suggest_from_trie
    from autocomplete_service.trie.builder import (
        PhraseTrieEntry,
        build_trie_from_phrases,
    )

    rows = [
        PhraseTrieEntry(
            id=f"p{i}", phrase=f"задишка варіант {chr(0x430 + i)}", source="system",
            impression_count=0, acceptance_count=0, last_accepted_at=None,
            specialty=None, section_hint=None,
        )
        for i in range(8)  # identical counters → identical scores
    ]
    orders = []
    for seed in (1, 7, 42):
        shuffled = rows[:]
        random.Random(seed).shuffle(shuffled)
        trie = build_trie_from_phrases(
            tenant_id="t", language="uk", user_id="u", rows=shuffled
        )
        got = suggest_from_trie(trie=trie, prefix="задишка", limit=8)
        orders.append([s.id for s in got])
    assert orders[0] == orders[1] == orders[2], f"non-deterministic: {orders}"


def test_diversity_distance_4_pair_survives():
    from autocomplete_service.ranking import PhraseRecord, diversity_filter

    def rec(id_, phrase):
        return PhraseRecord(
            id=id_, phrase=phrase, source="system",
            impression_count=0, acceptance_count=0, last_accepted_at=None,
        )

    # suffixes "abcd" vs "wxyz1" — Levenshtein 5 > 3 → both survive;
    # "abcd" vs "abce" — distance 1 → collapsed.
    ranked = [
        (rec("a", "x abcd"), 0.3, "abcd"),
        (rec("b", "x abce"), 0.2, "abce"),
        (rec("c", "x wxyz1"), 0.1, "wxyz1"),
    ]
    kept = diversity_filter(ranked, levenshtein_threshold=3)
    assert [r.id for r, _, _ in kept] == ["a", "c"]
