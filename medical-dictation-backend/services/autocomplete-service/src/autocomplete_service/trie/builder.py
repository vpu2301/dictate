"""Build a per-tenant trie from the phrase corpus.

The trie is the hot-path data structure: a prefix walk yields up to
the first K candidates for ranking. Sprint-10 uses Python dicts
keyed by prefix→list (1–6 char prefixes) for very small per-call
cost; production at larger scale will swap in marisa-trie's
``Trie.iter_prefix(prefix)`` if memory pressure rises.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

MAX_PREFIX_LEN = 6
TOP_K_PER_PREFIX = 20


@dataclass(frozen=True, slots=True)
class PhraseTrieEntry:
    id: str
    phrase: str
    source: str
    impression_count: int
    acceptance_count: int
    last_accepted_at: datetime | None
    specialty: str | None
    section_hint: str | None


@dataclass(slots=True)
class TenantTrie:
    """Compact prefix→top-K-candidates map. Built once per
    (tenant, language, user) tuple and cached in Redis."""

    tenant_id: str
    language: str
    user_id: str
    # Lower-cased prefix (length 1..MAX_PREFIX_LEN) → ordered list of entry ids
    prefix_to_ids: dict[str, list[str]]
    entries: dict[str, PhraseTrieEntry]
    built_at_unix: float

    def candidates_for(self, prefix: str, *, k: int = TOP_K_PER_PREFIX) -> list[PhraseTrieEntry]:
        if not prefix:
            return []
        key = prefix.lower()[:MAX_PREFIX_LEN]
        ids = self.prefix_to_ids.get(key)
        if not ids:
            # Fall back to scanning entries whose phrase starts with the
            # prefix when no exact prefix-bucket hit (e.g. prefix > 6 chars).
            full = prefix.lower()
            out: list[PhraseTrieEntry] = []
            for e in self.entries.values():
                if e.phrase.lower().startswith(full):
                    out.append(e)
                    if len(out) >= k * 2:
                        break
            return out[:k]
        return [self.entries[i] for i in ids[:k] if i in self.entries]


def build_trie_from_phrases(
    *,
    tenant_id: str,
    language: str,
    user_id: str,
    rows: Iterable[PhraseTrieEntry],
) -> TenantTrie:
    entries: dict[str, PhraseTrieEntry] = {}
    by_prefix: dict[str, list[tuple[str, float]]] = {}

    for e in rows:
        entries[e.id] = e
        # Score for the per-prefix top-K bucket — uses a coarse
        # acceptance-rate-style heuristic that the full ranker
        # refines later. This keeps the trie itself language-agnostic.
        coarse = (
            {"user": 1.0, "tenant": 0.6, "system": 0.3}.get(e.source, 0.3)
            * (e.acceptance_count + 1) / (e.impression_count + 10)
        )
        phrase_lower = e.phrase.lower()
        for length in range(1, min(MAX_PREFIX_LEN, len(phrase_lower)) + 1):
            key = phrase_lower[:length]
            by_prefix.setdefault(key, []).append((e.id, coarse))

    prefix_to_ids: dict[str, list[str]] = {}
    for k, lst in by_prefix.items():
        lst.sort(key=lambda x: x[1], reverse=True)
        prefix_to_ids[k] = [eid for eid, _ in lst[:TOP_K_PER_PREFIX]]

    return TenantTrie(
        tenant_id=tenant_id,
        language=language,
        user_id=user_id,
        prefix_to_ids=prefix_to_ids,
        entries=entries,
        built_at_unix=datetime.now(timezone.utc).timestamp(),
    )
