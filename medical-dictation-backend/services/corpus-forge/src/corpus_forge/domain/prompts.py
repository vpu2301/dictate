"""ASR prompt derivation — derive them, don't write them (sprint plan §7).

Top distinctive terms by tf-idf over the ACCEPTED corpus per
(language, specialty[, section]) cell → candidate prompt ≤224 tokens.
Tight beats padded: Whisper biases away from anything absent from the
prompt. A prompt ships only if it beats the incumbent on the eval set
(`delta_pp > 0`), never on vibes.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

from corpus_forge.domain.normalize import tokenize

ASR_PROMPT_MAX_TOKENS = 224  # docs/clinical-content/template-authoring.md


@dataclass(frozen=True, slots=True)
class PromptCell:
    language: str
    specialty: str
    section: str | None


def count_tokens(text: str) -> int:
    """Exact tiktoken count when available; the same len//4 fallback the
    template validator uses otherwise."""
    try:
        import tiktoken

        return len(tiktoken.get_encoding("cl100k_base").encode(text))
    except Exception:  # noqa: BLE001 - optional dependency, fallback is deliberate
        return max(1, len(text) // 4)


def tf_idf_terms(
    cell_phrases: dict[PromptCell, list[str]],
    *,
    target: PromptCell,
    top_n: int = 60,
) -> list[str]:
    """Terms distinctive to `target` relative to the other cells. Documents
    are cells, not phrases — idf rewards vocabulary other cells lack."""
    per_cell_tokens: dict[PromptCell, Counter[str]] = {}
    for cell, phrases in cell_phrases.items():
        counter: Counter[str] = Counter()
        for phrase in phrases:
            counter.update(t for t in tokenize(phrase.lower()) if len(t) > 2)
        per_cell_tokens[cell] = counter

    if target not in per_cell_tokens or not per_cell_tokens[target]:
        return []

    total_cells = len(per_cell_tokens)
    target_counts = per_cell_tokens[target]
    total_target = sum(target_counts.values())

    def score(token: str) -> float:
        tf = target_counts[token] / total_target
        containing = sum(1 for c in per_cell_tokens.values() if token in c)
        idf = math.log((1 + total_cells) / (1 + containing)) + 1.0
        return tf * idf

    ranked = sorted(target_counts, key=lambda t: (-score(t), t))
    return ranked[:top_n]


def build_prompt(terms: list[str], *, max_tokens: int = ASR_PROMPT_MAX_TOKENS) -> str:
    """Comma-joined term list, greedily filled up to the token budget."""
    prompt = ""
    for term in terms:
        candidate = f"{prompt}, {term}" if prompt else term
        if count_tokens(candidate) > max_tokens:
            break
        prompt = candidate
    return prompt


def promote_gate(*, incumbent_wer: float, candidate_wer: float) -> bool:
    """delta_pp > 0 or the prompt doesn't ship."""
    return (incumbent_wer - candidate_wer) * 100.0 > 0.0
