"""Word-level speaker attribution by majority overlap (sprint 14).

Pure functions: given the diarized speaker timeline and a word's
``TokenTiming`` start/end, decide which speaker uttered it. UNKNOWN when
the overlap evidence is ambiguous; ``None`` when the word's audio has not
been diarized yet (labels may trail text by one window — the FE renders
text immediately and colours it when the label lands).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .stream import SpeakerSegment

UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class AttributionPolicy:
    # A word must be covered by diarized speech for at least this share
    # of its duration to be attributed at all (else: UNKNOWN — spoken
    # where the diarizer heard no confident speech).
    min_coverage: float = 0.30
    # The winning speaker must own at least this share of the covered
    # overlap; a 50/50 straddle across a turn boundary yields UNKNOWN.
    majority_share: float = 0.65


DEFAULT_POLICY = AttributionPolicy()


def attribute_word(
    start_ms: int,
    end_ms: int,
    segments: list[SpeakerSegment],
    *,
    diarized_until_ms: int,
    policy: AttributionPolicy = DEFAULT_POLICY,
) -> tuple[str | None, float | None]:
    """Return ``(speaker, confidence)``.

    ``(None, None)``      — word extends past the diarized frontier: not yet.
    ``("UNKNOWN", 0.0)``  — diarized but ambiguous: never a guess.
    ``("S1"|"S2", conf)`` — majority-overlap winner; conf is the
                            overlap-weighted mean segment confidence
                            scaled by the winner's overlap share.
    """
    if end_ms > diarized_until_ms:
        return None, None
    duration = max(1, end_ms - start_ms)

    overlap_by_label: dict[str, float] = {}
    conf_weight_by_label: dict[str, float] = {}
    for seg in segments:
        lo = max(start_ms, seg.start_ms)
        hi = min(end_ms, seg.end_ms)
        if hi <= lo:
            continue
        share = (hi - lo) / duration
        overlap_by_label[seg.label] = overlap_by_label.get(seg.label, 0.0) + share
        conf_weight_by_label[seg.label] = (
            conf_weight_by_label.get(seg.label, 0.0) + share * seg.confidence
        )

    covered = sum(overlap_by_label.values())
    if covered < policy.min_coverage:
        return UNKNOWN, 0.0

    known = {k: v for k, v in overlap_by_label.items() if k != UNKNOWN}
    if not known:
        return UNKNOWN, 0.0
    winner = max(known, key=lambda k: known[k])
    if known[winner] / covered < policy.majority_share:
        return UNKNOWN, 0.0

    mean_conf = conf_weight_by_label[winner] / overlap_by_label[winner]
    confidence = float(max(0.0, min(1.0, mean_conf * (known[winner] / covered))))
    return winner, confidence
