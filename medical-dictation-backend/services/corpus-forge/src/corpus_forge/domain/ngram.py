"""N-gram extraction — the Python twin of the mining SQL's gates.

The authoritative gates run in SQL (adapters/mining.py, DPO-signed); this
module exists for the phrase-ification/generation paths and as the
defense-in-depth re-check on mined rows.
"""

from __future__ import annotations

from collections.abc import Iterator

from corpus_forge.domain.normalize import tokenize

MIN_TOKENS = 3
MAX_TOKENS = 12
MAX_CHARS = 80  # existing autocomplete_phrases validator limit


def ngrams(text: str) -> Iterator[str]:
    """Yield every 3–12-token n-gram of `text` that fits in 80 chars."""
    tokens = tokenize(text.lower())
    total = len(tokens)
    for n in range(MIN_TOKENS, MAX_TOKENS + 1):
        for start in range(total - n + 1):
            gram = " ".join(tokens[start : start + n])
            if len(gram) <= MAX_CHARS:
                yield gram


def within_gates(phrase: str) -> bool:
    """Length gates only — PII/k-anonymity are separate, deliberate steps."""
    token_count = len(tokenize(phrase))
    return MIN_TOKENS <= token_count <= MAX_TOKENS and 1 <= len(phrase) <= MAX_CHARS
