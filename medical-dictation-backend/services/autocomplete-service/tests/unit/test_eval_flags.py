"""Hallucination quarantine (corpus-v2 §4 P0-3).

The acceptance criterion is named in the backlog: the v1 utterance whose
hypothesis is "Дякую за перегляд!" must flag itself, without anyone
noticing it by eye first.
"""

from __future__ import annotations

from autocomplete_service import eval_flags


def test_the_known_youtube_signoff_flags_itself():
    """§4 P0-3's stated acceptance criterion. Whisper emits this on silence;
    at WER 1.0 the arithmetic alone would call it an ordinary bad take."""
    assert eval_flags.detect(wer=1.0, hypothesis="Дякую за перегляд!") == [
        eval_flags.FLAG_KNOWN_HALLUCINATION
    ]


def test_the_stoplist_matches_regardless_of_case_and_punctuation():
    assert eval_flags.FLAG_KNOWN_HALLUCINATION in eval_flags.detect(
        wer=0.4, hypothesis="THANKS FOR WATCHING!!!"
    )


def test_a_stoplist_phrase_is_caught_even_at_a_plausible_wer():
    """A sign-off appended to a real utterance still contaminates it, and
    at WER 0.4 nothing else would notice."""
    assert eval_flags.FLAG_KNOWN_HALLUCINATION in eval_flags.detect(
        wer=0.4, hypothesis="тиск сто сорок на дев'яносто дякую за перегляд"
    )


def test_more_errors_than_reference_words_is_impossible_by_mishearing():
    assert eval_flags.detect(wer=1.75, hypothesis="щось геть інше") == [
        eval_flags.FLAG_WER_OVER_100
    ]


def test_a_clean_take_carries_no_flags():
    assert eval_flags.detect(
        wer=0.2, hypothesis="скарги на задишку", speech_ms=3000, duration_ms=4000
    ) == []


def test_under_a_second_of_speech_and_mostly_silence_are_separate_signals():
    flags = eval_flags.detect(
        wer=0.2, hypothesis="ок", speech_ms=400, duration_ms=4000
    )
    assert flags == [
        eval_flags.FLAG_SPEECH_TOO_SHORT,
        eval_flags.FLAG_MOSTLY_SILENCE,
    ]


def test_a_long_take_that_is_mostly_silence_still_flags():
    assert eval_flags.detect(
        wer=0.2, hypothesis="ок", speech_ms=2000, duration_ms=9000
    ) == [eval_flags.FLAG_MOSTLY_SILENCE]


def test_vad_flags_cannot_fire_when_the_engine_reported_no_vad():
    """"We did not look" and "we looked and it was fine" are different
    states; only one of them is reassuring, so the absence of VAD data
    produces no flag and ``vad_checked`` says why."""
    assert eval_flags.detect(wer=0.2, hypothesis="ок", speech_ms=None) == []
    assert eval_flags.vad_checked(None) is False
    assert eval_flags.vad_checked(0) is True


def test_partition_splits_counted_from_quarantined():
    items = [
        {"script_id": "a", "flags": []},
        {"script_id": "b", "flags": ["wer_over_100"]},
        {"script_id": "c"},
    ]
    counted, flagged = eval_flags.partition(items)
    assert [i["script_id"] for i in counted] == ["a", "c"]
    assert [i["script_id"] for i in flagged] == ["b"]
