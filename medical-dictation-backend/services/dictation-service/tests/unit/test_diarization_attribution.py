"""Word-level speaker attribution tests (pure function, no models)."""

from __future__ import annotations

from dictation_service.diarization.attribution import UNKNOWN, attribute_word
from dictation_service.diarization.stream import SpeakerSegment


def test_word_fully_inside_one_segment() -> None:
    segments = [SpeakerSegment(0, 1000, "S1", 0.9)]
    speaker, conf = attribute_word(200, 700, segments, diarized_until_ms=1000)
    assert speaker == "S1"
    assert conf is not None and conf > 0.0
    # Full coverage by a single segment: confidence == segment confidence.
    assert abs(conf - 0.9) < 1e-9


def test_word_past_diarized_frontier_is_pending() -> None:
    segments = [SpeakerSegment(0, 2000, "S1", 0.9)]
    assert attribute_word(1500, 2500, segments, diarized_until_ms=2000) == (None, None)


def test_fifty_fifty_straddle_is_unknown() -> None:
    segments = [
        SpeakerSegment(0, 1000, "S1", 0.9),
        SpeakerSegment(1000, 2000, "S2", 0.9),
    ]
    speaker, conf = attribute_word(500, 1500, segments, diarized_until_ms=2000)
    assert speaker == UNKNOWN
    assert conf == 0.0


def test_insufficient_coverage_is_unknown() -> None:
    # Only 10% of the word overlaps diarized speech (< min_coverage 0.30).
    segments = [SpeakerSegment(0, 100, "S1", 0.9)]
    speaker, conf = attribute_word(0, 1000, segments, diarized_until_ms=2000)
    assert speaker == UNKNOWN
    assert conf == 0.0


def test_mostly_unknown_labeled_coverage_is_unknown() -> None:
    segments = [
        SpeakerSegment(0, 900, "UNKNOWN", 0.0),
        SpeakerSegment(900, 1000, "S1", 0.9),
    ]
    speaker, conf = attribute_word(0, 1000, segments, diarized_until_ms=1000)
    assert speaker == UNKNOWN
    assert conf == 0.0


def test_only_unknown_coverage_is_unknown() -> None:
    segments = [SpeakerSegment(0, 1000, "UNKNOWN", 0.0)]
    speaker, conf = attribute_word(100, 600, segments, diarized_until_ms=1000)
    assert speaker == UNKNOWN
    assert conf == 0.0


def test_confidence_scales_with_segment_confidence() -> None:
    high = [SpeakerSegment(0, 1000, "S1", 0.9)]
    low = [SpeakerSegment(0, 1000, "S1", 0.5)]
    _, conf_high = attribute_word(100, 900, high, diarized_until_ms=1000)
    _, conf_low = attribute_word(100, 900, low, diarized_until_ms=1000)
    assert conf_high is not None and conf_low is not None
    assert conf_high > conf_low
    assert abs(conf_low - 0.5) < 1e-9


def test_confidence_scales_with_overlap_share() -> None:
    full = [SpeakerSegment(0, 1000, "S1", 1.0)]
    partial = [
        SpeakerSegment(0, 800, "S1", 1.0),
        SpeakerSegment(800, 1000, "UNKNOWN", 0.0),
    ]
    speaker_full, conf_full = attribute_word(0, 1000, full, diarized_until_ms=1000)
    speaker_partial, conf_partial = attribute_word(0, 1000, partial, diarized_until_ms=1000)
    assert speaker_full == "S1" and speaker_partial == "S1"
    assert conf_full is not None and conf_partial is not None
    # Winner share 0.8 of covered overlap scales confidence down: 1.0 -> 0.8.
    assert conf_full > conf_partial
    assert abs(conf_full - 1.0) < 1e-9
    assert abs(conf_partial - 0.8) < 1e-9
