"""Near-duplicate gate — same rapidfuzz Levenshtein-≤3 rule the sprint-10
serve-time diversity guard uses. Deduping at ingest is free; deduping at
serve time means we paid review budget for rows the ranker throws away.
"""

from __future__ import annotations

from collections.abc import Iterable

from rapidfuzz.distance import Levenshtein

from corpus_forge.domain.normalize import dedupe_key

LEVENSHTEIN_THRESHOLD = 3  # matches autocomplete_service.ranking.diversity_filter


def is_near_duplicate(phrase: str, existing: Iterable[str]) -> bool:
    key = dedupe_key(phrase)
    return any(
        Levenshtein.distance(key, dedupe_key(other)) <= LEVENSHTEIN_THRESHOLD
        for other in existing
    )


def dedupe_batch(phrases: list[str], *, against: Iterable[str] = ()) -> list[str]:
    """Keep first occurrence; drop exact + near duplicates, in-batch and
    against the accepted corpus. Order-preserving and deterministic."""
    kept: list[str] = []
    kept_keys: list[str] = []
    corpus_keys = [dedupe_key(p) for p in against]
    for phrase in phrases:
        key = dedupe_key(phrase)
        if any(
            Levenshtein.distance(key, seen) <= LEVENSHTEIN_THRESHOLD
            for seen in (*corpus_keys, *kept_keys)
        ):
            continue
        kept.append(phrase)
        kept_keys.append(key)
    return kept
