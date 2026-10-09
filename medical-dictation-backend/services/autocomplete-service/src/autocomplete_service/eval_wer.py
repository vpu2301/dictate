"""WER/CER scoring — the service-side twin of ``scripts/eval/wer_lib.py``.

The measurement CONTRACT (tokenisation, Levenshtein, the empty-reference
edge case) is defined once in ``docs/eval/wer-methodology.md`` and has an
executable form in ``scripts/eval/wer_lib.py``, which the nightly release
gate runs. This module is that same arithmetic inside the service, because
a service may not import from ``scripts/`` (it is not a workspace package
and does not ship in the image).

Duplication is a cost; silent divergence would be a worse one, so
``tests/unit/test_eval_wer.py`` asserts token-for-token parity against
``wer_lib`` on a shared case table. Change one, the test fails until you
change the other.

Stdlib only, deterministic, no I/O — the same properties that make the CLI
version unit-testable without a GPU.
"""

from __future__ import annotations

import re
from typing import Final

# Lowercase + strip non-alphanumeric, Unicode-aware so Cyrillic survives.
# Ukrainian case endings are deliberately NOT stripped: "інфаркту" vs
# "інфаркт" is a real error and must count (methodology §WER).
_TOKEN_STRIP: Final = re.compile(r"[^\w']+", flags=re.UNICODE)


def tokenize(text: str) -> list[str]:
    """Lowercase, strip punctuation (keep apostrophes), whitespace split."""
    return _TOKEN_STRIP.sub(" ", text.lower()).split()


def _levenshtein(ref: list[str], hyp: list[str]) -> int:
    n, m = len(ref), len(hyp)
    if n == 0:
        return m
    if m == 0:
        return n
    prev = list(range(m + 1))
    for i in range(1, n + 1):
        curr = [i] + [0] * m
        for j in range(1, m + 1):
            cost = 0 if ref[i - 1] == hyp[j - 1] else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[m]


def wer(reference: str, hypothesis: str) -> tuple[float, int]:
    """Levenshtein word error rate. Returns ``(wer, reference_word_count)``.

    An empty reference scores 0.0 against an empty hypothesis and 1.0
    against anything else (methodology §Edge cases) — a full insertion.
    """
    ref = tokenize(reference)
    hyp = tokenize(hypothesis)
    n = len(ref)
    if n == 0:
        return (0.0 if not hyp else 1.0), 0
    return _levenshtein(ref, hyp) / n, n


def cer(reference: str, hypothesis: str) -> tuple[float, int]:
    """Character error rate, case-insensitive, spaces preserved.

    The tiebreaker for utterances a single substitution dominates: one wrong
    word in a five-word line is WER 0.2 whether the model heard a near-miss
    or a different drug.
    """
    ref = list(reference.lower())
    hyp = list(hypothesis.lower())
    n = len(ref)
    if n == 0:
        return (0.0 if not hyp else 1.0), 0
    return _levenshtein(ref, hyp) / n, n


def aggregate(scored: list[dict[str, object]]) -> dict[str, object]:
    """Roll per-utterance scores into a run summary.

    WEIGHTED BY REFERENCE WORDS, not averaged over utterances: a corpus of
    one twenty-word line and one three-word line does not get to have the
    short line count as half the evidence. This is what the methodology
    calls corpus WER, and it is why ``ref_words`` is stored per item.

    ``scored`` items are dicts with ``wer``/``cer``/``ref_words``/``subset``/
    ``language``; anything unscored (failed, skipped) must be filtered out
    by the caller — an unscored utterance is absent from the numerator AND
    the denominator, never a zero.
    """
    def _roll(items: list[dict[str, object]]) -> dict[str, object]:
        words = sum(int(i["ref_words"] or 0) for i in items)
        chars = sum(len(str(i.get("reference") or "")) for i in items)
        if words == 0:
            return {"utterances": len(items), "ref_words": 0, "wer": None, "cer": None}
        wer_num = sum(float(i["wer"]) * int(i["ref_words"] or 0) for i in items)
        cer_num = sum(
            float(i["cer"]) * len(str(i.get("reference") or "")) for i in items
        )
        return {
            "utterances": len(items),
            "ref_words": words,
            "wer": round(wer_num / words, 4),
            "cer": round(cer_num / chars, 4) if chars else None,
        }

    by_subset: dict[str, list[dict[str, object]]] = {}
    by_language: dict[str, list[dict[str, object]]] = {}
    for item in scored:
        # A baseline line has no subset; it is still part of the overall
        # number, it just has no adversarial bucket of its own.
        by_subset.setdefault(str(item.get("subset") or "baseline"), []).append(item)
        by_language.setdefault(str(item.get("language") or "?"), []).append(item)

    return {
        "overall": _roll(scored),
        "by_subset": {k: _roll(v) for k, v in sorted(by_subset.items())},
        "by_language": {k: _roll(v) for k, v in sorted(by_language.items())},
    }
