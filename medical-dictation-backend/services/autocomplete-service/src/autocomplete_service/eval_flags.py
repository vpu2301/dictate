"""Hallucination quarantine — corpus-v2 §1.1 and §4 P0-3.

The v1 run has an utterance scoring 100% WER whose hypothesis is "Дякую за
перегляд!" — a YouTube sign-off Whisper emits when it is handed silence or
a fragment too short to decode. Another scores 175%. Neither is a
measurement of clinical ASR quality; both are measurements of a recording
that went wrong, and averaging them into the corpus number moves it by
several points in the direction of "the model is bad" when the truth is
"that take needs redoing".

So flagged duplicates are pulled OUT of every average and listed on their
own, with the reason. Nothing is deleted and no score is discarded — the
score is still stored, it is simply not allowed to vote. The operator sees a
short list of takes to re-record, which is the action the flag exists to
prompt.

FOUR SIGNALS, and they are independent on purpose:

  wer_over_100         more errors than reference words: the model produced
                       text unrelated to what was said. Arithmetically
                       impossible to reach by mishearing a word.
  known_hallucination  the hypothesis contains a phrase from the stoplist —
                       the sign-offs and subtitle credits Whisper falls back
                       to. Caught even at WER 40%, where the arithmetic
                       alone would not notice.
  speech_too_short     under a second of speech by the engine's own VAD.
  mostly_silence       over half the take is silence.

The last two need ``vad_seconds_speech`` from the ASR metadata. When the
engine does not report it, they CANNOT fire — and the item is reported as
unchecked rather than as clean, because "we did not look" and "we looked and
it was fine" are different states and only one of them is reassuring.

Stdlib only, no I/O.
"""

from __future__ import annotations

from typing import Any, Final

from .eval_normalize import fold, hallucination_stoplist

FLAG_WER_OVER_100: Final = "wer_over_100"
FLAG_KNOWN_HALLUCINATION: Final = "known_hallucination"
FLAG_SPEECH_TOO_SHORT: Final = "speech_too_short"
FLAG_MOSTLY_SILENCE: Final = "mostly_silence"

#: §4 P0-3: "мовлення < 1 с за VAD".
MIN_SPEECH_MS: Final = 1000
#: §4 P0-3: "тиша > 50% дубля".
MAX_SILENCE_RATIO: Final = 0.5

ALL_FLAGS: Final[tuple[str, ...]] = (
    FLAG_WER_OVER_100,
    FLAG_KNOWN_HALLUCINATION,
    FLAG_SPEECH_TOO_SHORT,
    FLAG_MOSTLY_SILENCE,
)


def detect(
    *,
    wer: float | None,
    hypothesis: str | None,
    speech_ms: int | None = None,
    duration_ms: int | None = None,
) -> list[str]:
    """Flags for one scored utterance, stable order. Empty = clean.

    ``wer`` is the RAW rate: normalisation can only lower a score, so a
    normalised WER below 1.0 would hide a hypothesis that had nothing to do
    with the reference.
    """
    flags: list[str] = []

    if wer is not None and wer > 1.0:
        flags.append(FLAG_WER_OVER_100)

    if hypothesis:
        folded = fold(hypothesis)
        if any(phrase and phrase in folded for phrase in hallucination_stoplist()):
            flags.append(FLAG_KNOWN_HALLUCINATION)

    if speech_ms is not None:
        if speech_ms < MIN_SPEECH_MS:
            flags.append(FLAG_SPEECH_TOO_SHORT)
        if duration_ms and (1.0 - speech_ms / duration_ms) > MAX_SILENCE_RATIO:
            flags.append(FLAG_MOSTLY_SILENCE)

    return flags


def partition(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split scored items into (counted, flagged) by their stored flags."""
    counted = [i for i in items if not (i.get("flags") or [])]
    flagged = [i for i in items if (i.get("flags") or [])]
    return counted, flagged


def vad_checked(speech_ms: int | None) -> bool:
    """Whether the VAD-based flags were able to run at all."""
    return speech_ms is not None
