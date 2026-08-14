"""Fluency pre-filter for generated candidates (ADR-0044 §4).

kenlm-uk when a model is configured; otherwise a deterministic heuristic.
The fallback is honest, not silent: `FluencyFilter.name` is recorded in the
release manifest so a release states which filter actually ran.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Protocol

from corpus_forge.domain.normalize import tokenize

_LETTERS: Final = re.compile(r"[^\W\d_]", re.UNICODE)
_MIXED_SCRIPT_TOKEN: Final = re.compile(
    r"[а-яїієґА-ЯЇІЄҐ][a-zA-Z]|[a-zA-Z][а-яїієґА-ЯЇІЄҐ]"
)


class FluencyFilter(Protocol):
    @property
    def name(self) -> str: ...

    def keep(self, phrase: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class HeuristicFluencyFilter:
    """Deterministic sanity gates: token bounds, letter density, no
    3-in-a-row repeats, no mixed-script tokens (transliteration artifacts)."""

    name: str = "heuristic-v1"

    def keep(self, phrase: str) -> bool:
        tokens = tokenize(phrase)
        if not 3 <= len(tokens) <= 12:
            return False
        if len(_LETTERS.findall(phrase)) / max(len(phrase), 1) < 0.5:
            return False
        lowered = [t.lower() for t in tokens]
        if any(lowered[i] == lowered[i + 1] == lowered[i + 2] for i in range(len(lowered) - 2)):
            return False
        return not any(_MIXED_SCRIPT_TOKEN.search(t) for t in tokens)


class KenlmFluencyFilter:
    """Perplexity filter: drops the bottom decile of a batch. Requires the
    optional `kenlm` package and a Ukrainian LM binary (pinned separately)."""

    def __init__(self, model_path: str) -> None:
        import kenlm  # optional dependency, deliberately imported lazily

        self._model = kenlm.Model(model_path)
        self._name = f"kenlm:{model_path.rsplit('/', 1)[-1]}"

    @property
    def name(self) -> str:
        return self._name

    def keep(self, phrase: str) -> bool:  # pragma: no cover - needs model file
        return True  # per-phrase keep is decided batch-wise; see filter_batch

    def filter_batch(self, phrases: list[str]) -> list[str]:  # pragma: no cover
        if len(phrases) < 10:
            return phrases
        scored = sorted(
            phrases, key=lambda p: self._model.perplexity(" ".join(tokenize(p)))
        )
        cutoff = len(scored) - len(scored) // 10
        keep_set = set(scored[:cutoff])
        return [p for p in phrases if p in keep_set]


def build_fluency_filter(kenlm_model_path: str) -> FluencyFilter:
    if kenlm_model_path:
        return KenlmFluencyFilter(kenlm_model_path)
    return HeuristicFluencyFilter()
