"""Ranking unit tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    base = dict(
        id="a", phrase="хворий поступив зі скаргами", source="system",
        impression_count=0, acceptance_count=0, last_accepted_at=None,
    )
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
    today = datetime.now(timezone.utc)
    assert recency_boost(today, now=today) > 1.0
    week_ago = today - timedelta(days=7)
    assert recency_boost(week_ago, now=today) > 1.0
    assert recency_boost(week_ago, now=today) < recency_boost(today, now=today)


def test_recency_boost_old_neutral():
    today = datetime.now(timezone.utc)
    old = today - timedelta(days=60)
    assert recency_boost(old, now=today) == 1.0
    assert recency_boost(None) == 1.0


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
    ranked = [(a, 0.9, "alpha-beta-gamma"),
              (b, 0.8, "alpha-beta-gamna"),
              (c, 0.7, "totally different")]
    kept = diversity_filter(ranked, levenshtein_threshold=3)
    assert len(kept) == 2
    assert kept[0][0].id == "a"
    assert kept[1][0].id == "c"


def test_confidence_in_unit_interval():
    assert 0.0 <= confidence(0.0) <= 1.0
    assert 0.0 <= confidence(1.0) <= 1.0
    assert confidence(1.0) > confidence(0.0)
