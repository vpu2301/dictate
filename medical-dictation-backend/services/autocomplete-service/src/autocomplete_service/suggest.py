"""Suggest dispatcher — trie path + snippet path.

Pure domain functions; the router translates to HTTP shapes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from autocomplete_service.ranking import (
    PhraseRecord,
    confidence,
    diversity_filter,
    score,
)
from autocomplete_service.trie.builder import TenantTrie

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Suggestion:
    id: str
    kind: str  # 'phrase' | 'snippet'
    text: str
    completion: str
    source: str
    confidence: float
    cursor_offset: int | None = None


def is_snippet_prefix(prefix: str) -> bool:
    return prefix.startswith("/")


def extract_snippet_trigger(prefix: str) -> str:
    """Strip leading slash; lower-cased trigger (1-32 chars)."""
    return prefix.lstrip("/").strip().lower()[:32]


def suggest_from_trie(
    *,
    trie: TenantTrie,
    prefix: str,
    limit: int,
    now: datetime | None = None,
) -> list[Suggestion]:
    candidates = trie.candidates_for(prefix)
    if not candidates:
        return []

    full_prefix = prefix.lower()
    ranked: list[tuple[PhraseRecord, float, str]] = []
    for c in candidates:
        rec = PhraseRecord(
            id=c.id,
            phrase=c.phrase,
            source=c.source,
            impression_count=c.impression_count,
            acceptance_count=c.acceptance_count,
            last_accepted_at=c.last_accepted_at,
        )
        s = score(rec, now=now)
        # Suffix = what the FE will surface as ghost-text.
        phrase_lower = c.phrase.lower()
        suffix = c.phrase[len(prefix) :] if phrase_lower.startswith(full_prefix) else c.phrase
        ranked.append((rec, s, suffix))

    ranked.sort(key=lambda t: t[1], reverse=True)
    deduped = diversity_filter(ranked, levenshtein_threshold=3)
    top = deduped[:limit]
    return [
        Suggestion(
            id=rec.id,
            kind="phrase",
            text=rec.phrase,
            completion=suffix,
            source=rec.source,
            confidence=confidence(s),
        )
        for rec, s, suffix in top
    ]


def snippet_suggestion(
    *,
    snippet_id: str,
    expansion: str,
    cursor_position: int,
    source: str,
    trigger: str,
) -> Suggestion:
    return Suggestion(
        id=snippet_id,
        kind="snippet",
        text=expansion,
        completion=expansion,
        source=source,
        confidence=1.0,
        cursor_offset=cursor_position,
    )
