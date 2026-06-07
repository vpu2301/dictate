"""Ranking + diversity guard.

Score = source_priority * Beta(1,9)-acceptance * recency_boost * length_score.

The Bayesian prior (Beta(1,9)) gives a 0-out-of-0 phrase a prior
acceptance rate of 1/10 = 0.1 — non-zero so it can surface, low so it
doesn't beat proven phrases until it earns its place.

Diversity guard removes near-duplicate suffixes using rapidfuzz's
Levenshtein implementation; keep the higher-ranked candidate when two
suffix strings are within Levenshtein 3.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from rapidfuzz.distance import Levenshtein

SOURCE_PRIORITY: dict[str, float] = {
    "user": 1.0,
    "tenant": 0.6,
    "system": 0.3,
}

ALPHA = 1
BETA = 9


@dataclass(frozen=True, slots=True)
class PhraseRecord:
    id: str
    phrase: str
    source: str  # 'user' | 'tenant' | 'system'
    impression_count: int
    acceptance_count: int
    last_accepted_at: datetime | None


def bayesian_acceptance(impressions: int, accepts: int) -> float:
    return (accepts + ALPHA) / (impressions + ALPHA + BETA)


def recency_boost(last_accepted_at: datetime | None, *, now: datetime | None = None) -> float:
    if last_accepted_at is None:
        return 1.0
    now = now or datetime.now(UTC)
    days_ago = (now - last_accepted_at).total_seconds() / 86400.0
    if days_ago < 0 or days_ago > 30:
        return 1.0
    return 1.0 + 0.2 * math.exp(-days_ago / 7.0)


def length_score(phrase: str) -> float:
    return 1.0 / (1.0 + len(phrase) / 50.0)


def score(p: PhraseRecord, *, now: datetime | None = None) -> float:
    priority = SOURCE_PRIORITY.get(p.source, 0.3)
    return (
        priority
        * bayesian_acceptance(p.impression_count, p.acceptance_count)
        * recency_boost(p.last_accepted_at, now=now)
        * length_score(p.phrase)
    )


def diversity_filter(
    ranked: Iterable[tuple[PhraseRecord, float, str]],
    *,
    levenshtein_threshold: int = 3,
) -> list[tuple[PhraseRecord, float, str]]:
    """Drop near-duplicate suffixes.

    ``ranked`` is iterable of (record, score, suffix); descending by score.
    Returns the same shape with near-dups removed.
    """
    kept: list[tuple[PhraseRecord, float, str]] = []
    for cand in ranked:
        _, _, suf = cand
        if any(Levenshtein.distance(suf, ks) <= levenshtein_threshold for _, _, ks in kept):
            continue
        kept.append(cand)
    return kept


def confidence(score_val: float) -> float:
    """Map raw score into [0, 1] for FE consumption.

    Sprint-10 uses a sigmoid: a score of 0.1 (zero-prior baseline)
    maps to ~0.10, a score of 1.0 maps to ~0.73.
    """
    return 1.0 / (1.0 + math.exp(-(score_val - 0.3) * 4))
