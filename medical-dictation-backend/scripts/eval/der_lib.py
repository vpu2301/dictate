"""Diarization scoring library (sprint 14, ADR-0034).

Frame-based DER with the standard ±collar around reference turn
boundaries and optimal (injective) mapping between hypothesis labels
{S1,S2} and reference speakers. UNKNOWN hypothesis frames are unlabeled:
they count as MISS over reference speech — honesty is penalised less
than a wrong guess would be in the clinic, but it is never free.

Also scores word-level attribution: reference turns are split into
pseudo-words (time allocated proportionally to token length — exact
turn boundaries are generator ground truth, per-word timing inside a
turn is synthetic), each attributed via the production
``attribute_word`` merge and compared to the reference speaker.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations
from typing import Any

FRAME_MS = 10


@dataclass(frozen=True)
class DerResult:
    der: float
    miss: float
    false_alarm: float
    confusion: float
    ref_speech_ms: int
    mapping: dict[str, str]  # hyp label -> ref speaker


def _frames(duration_ms: int) -> int:
    return duration_ms // FRAME_MS


def _paint(intervals: list[tuple[int, int, str]], n_frames: int) -> list[str | None]:
    canvas: list[str | None] = [None] * n_frames
    for start_ms, end_ms, label in intervals:
        for f in range(max(0, start_ms // FRAME_MS), min(n_frames, end_ms // FRAME_MS)):
            canvas[f] = label
    return canvas


def compute_der(
    ref_turns: list[dict[str, Any]],
    hyp_segments: list[tuple[int, int, str]],
    *,
    duration_ms: int,
    collar_ms: int = 250,
) -> DerResult:
    n = _frames(duration_ms)
    ref = _paint([(t["start_ms"], t["end_ms"], t["speaker"]) for t in ref_turns], n)
    # Collar: frames within ±collar of any reference boundary are excluded.
    scored = [True] * n
    for t in ref_turns:
        for edge in (t["start_ms"], t["end_ms"]):
            for f in range((edge - collar_ms) // FRAME_MS, (edge + collar_ms) // FRAME_MS + 1):
                if 0 <= f < n:
                    scored[f] = False

    hyp_labels = sorted({label for _, _, label in hyp_segments if label != "UNKNOWN"})
    ref_speakers = sorted({t["speaker"] for t in ref_turns})

    best: DerResult | None = None
    # Try every injective hyp->ref assignment (≤2 hyp labels ⇒ tiny).
    candidate_maps: list[dict[str, str]] = []
    if not hyp_labels:
        candidate_maps.append({})
    else:
        for perm in permutations(ref_speakers, min(len(hyp_labels), len(ref_speakers))):
            candidate_maps.append(dict(zip(hyp_labels, perm, strict=False)))
    for mapping in candidate_maps:
        hyp = _paint(
            [(s, e, mapping.get(label, "UNKNOWN")) for s, e, label in hyp_segments],
            n,
        )
        miss = fa = conf = ref_ms = 0
        for f in range(n):
            if not scored[f]:
                continue
            r, h = ref[f], hyp[f]
            if r is not None:
                ref_ms += FRAME_MS
                if h is None or h == "UNKNOWN":
                    miss += FRAME_MS
                elif h != r:
                    conf += FRAME_MS
            elif h is not None and h != "UNKNOWN":
                fa += FRAME_MS
        denom = max(1, ref_ms)
        der = (miss + fa + conf) / denom
        result = DerResult(
            der=round(der, 4),
            miss=round(miss / denom, 4),
            false_alarm=round(fa / denom, 4),
            confusion=round(conf / denom, 4),
            ref_speech_ms=ref_ms,
            mapping=mapping,
        )
        if best is None or result.der < best.der:
            best = result
    assert best is not None
    return best


def pseudo_words(ref_turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split each reference turn into word tokens with time allocated
    proportionally to token length (+1 for the trailing space)."""
    words: list[dict[str, Any]] = []
    for t in ref_turns:
        tokens = [w for w in t["text"].split() if w]
        if not tokens:
            continue
        weights = [len(w) + 1 for w in tokens]
        total_w = sum(weights)
        span = t["end_ms"] - t["start_ms"]
        cursor = float(t["start_ms"])
        for tok, w in zip(tokens, weights, strict=True):
            dur = span * w / total_w
            words.append(
                {
                    "text": tok,
                    "speaker": t["speaker"],
                    "start_ms": int(cursor),
                    "end_ms": int(cursor + dur),
                }
            )
            cursor += dur
    return words


@dataclass(frozen=True)
class AttributionResult:
    total_words: int
    labeled: int
    correct: int
    unknown: int
    pending: int  # attribution returned None (past diarized frontier)
    strict_accuracy: float  # correct / total (UNKNOWN + pending count against)
    labeled_precision: float  # correct / labeled
    unknown_rate: float


def score_attribution(
    words: list[dict[str, Any]],
    attributions: list[tuple[str | None, float | None]],
    mapping: dict[str, str],
) -> AttributionResult:
    total = len(words)
    labeled = correct = unknown = pending = 0
    for word, (label, _conf) in zip(words, attributions, strict=True):
        if label is None:
            pending += 1
        elif label == "UNKNOWN":
            unknown += 1
        else:
            labeled += 1
            if mapping.get(label) == word["speaker"]:
                correct += 1
    return AttributionResult(
        total_words=total,
        labeled=labeled,
        correct=correct,
        unknown=unknown,
        pending=pending,
        strict_accuracy=round(correct / max(1, total), 4),
        labeled_precision=round(correct / max(1, labeled), 4),
        unknown_rate=round(unknown / max(1, total), 4),
    )
