"""Doctor/patient mapping inference tests (pure heuristics, no models).

Lexicon-sensitive words used below: "аналіз" matches the ``аналіз*``
prefix, "призначаю" matches ``призна*``, "чи"/"приймаєте" are exact
entries. "пацієнт", "мене", "болить" etc. hit nothing.
"""

from __future__ import annotations

from dictation_service.diarization.mapping import SpeakerMappingInference
from dictation_service.diarization.stream import SpeakerSegment

_DOCTOR_OPENER = "Призначаю аналіз крові."
_DOCTOR_QUESTION = "Чи приймаєте препарати?"
_PATIENT_WORDS = "мене болить голова вже два дні"

_HIT = "аналіз"  # 1 clinician-lexicon hit per token
_PLAIN = "добре"  # 0 hits


def _feed(inference: SpeakerMappingInference, speaker: str, hits: int, plain: int) -> None:
    for _ in range(hits):
        inference.observe_word(_HIT, speaker)
    for _ in range(plain):
        inference.observe_word(_PLAIN, speaker)


def test_doctor_opens_with_clinician_vocabulary() -> None:
    inference = SpeakerMappingInference(language="uk")
    inference.observe_segments(
        [
            SpeakerSegment(0, 6000, "S1", 0.9),
            SpeakerSegment(6000, 9000, "S2", 0.9),
        ]
    )
    inference.observe_word(_DOCTOR_OPENER, "S1")
    inference.observe_word(_DOCTOR_QUESTION, "S1")
    for word in _PATIENT_WORDS.split():
        inference.observe_word(word, "S2")

    hypothesis = inference.evaluate()
    assert hypothesis is not None
    assert hypothesis.mapping == {"S1": "doctor", "S2": "patient"}
    assert hypothesis.confidence >= 0.6
    assert inference.current == hypothesis


def test_patient_opens_but_doctor_vocab_dominates() -> None:
    # S1 opens the session (patient), S2 carries the clinician register.
    # Vocab weighs 0.65 vs opener 0.35, so the implementation still maps
    # the vocab-heavy S2 to doctor (evidence S2 = 0.65 vs S1 = 0.35,
    # confidence 0.77 >= emit threshold) — locked-in behavior.
    inference = SpeakerMappingInference(language="uk")
    inference.observe_segments([SpeakerSegment(0, 8000, "S1", 0.9)])
    for word in _PATIENT_WORDS.split():
        inference.observe_word(word, "S1")
    inference.observe_word(_DOCTOR_OPENER, "S2")
    inference.observe_word(_DOCTOR_QUESTION, "S2")

    hypothesis = inference.evaluate()
    assert hypothesis is not None
    assert hypothesis.mapping == {"S2": "doctor", "S1": "patient"}
    assert hypothesis.confidence >= 0.6


def test_freeze_is_permanent_and_authoritative() -> None:
    inference = SpeakerMappingInference(language="uk")
    inference.observe_word(_DOCTOR_OPENER, "S1")
    inference.observe_word(_DOCTOR_QUESTION, "S1")
    for word in _PATIENT_WORDS.split():
        inference.observe_word(word, "S2")

    inference.freeze({"S2": "doctor", "S1": "patient"})
    assert inference.evaluate() is None
    current = inference.current
    assert current is not None
    assert current.mapping == {"S2": "doctor", "S1": "patient"}
    assert current.confidence == 1.0

    # Post-freeze evidence is ignored entirely.
    inference.observe_word(_HIT, "S1")
    assert inference.evaluate() is None
    assert inference.current is not None
    assert inference.current.mapping == {"S2": "doctor", "S1": "patient"}


def test_flip_resistance_requires_strong_evidence() -> None:
    inference = SpeakerMappingInference(language="uk")
    # No opener evidence: both speakers get the neutral 0.5 share.
    _feed(inference, "S1", hits=2, plain=10)  # density 2/12
    _feed(inference, "S2", hits=1, plain=11)  # density 1/12

    first = inference.evaluate()
    assert first is not None
    assert first.mapping == {"S1": "doctor", "S2": "patient"}

    # Small evidence change (S1 diluted to near-parity): confidence drops
    # below the emit threshold -> nothing emitted, belief unchanged.
    _feed(inference, "S1", hits=0, plain=10)
    assert inference.evaluate() is None
    assert inference.current == first

    # Weak flip (S2 now modestly vocab-dominant): new confidence is below
    # first.confidence + flip_margin -> suppressed.
    _feed(inference, "S2", hits=3, plain=0)
    assert inference.evaluate() is None
    assert inference.current == first

    # Strong flip: S2 overwhelmingly clinician-register -> emitted.
    _feed(inference, "S2", hits=12, plain=0)
    flipped = inference.evaluate()
    assert flipped is not None
    assert flipped.mapping == {"S2": "doctor", "S1": "patient"}
    assert flipped.confidence >= first.confidence + inference.flip_margin
    assert inference.current == flipped


def test_unknown_and_none_speakers_are_ignored() -> None:
    inference = SpeakerMappingInference(language="uk")
    for _ in range(20):
        inference.observe_word(_HIT, "UNKNOWN")
        inference.observe_word(_PLAIN, None)
    # Only one real speaker afterwards -> still no hypothesis possible.
    _feed(inference, "S1", hits=6, plain=6)
    assert inference.evaluate() is None
    assert set(inference._words) == {"S1"}


# ── German ──────────────────────────────────────────────────────────

_DE_DOCTOR_OPENER = "Ich verschreibe Ibuprofen und überweise Sie."
_DE_DOCTOR_QUESTION = "Bitte untersuchen wir das Blutbild."
_DE_PATIENT_WORDS = "ich habe seit zwei Tagen Kopfschmerzen"
_DE_PLAIN = "gut"  # 0 hits


def test_german_lexicon_maps_the_clinician() -> None:
    inference = SpeakerMappingInference(language="de")
    inference.observe_segments(
        [
            SpeakerSegment(0, 6000, "S1", 0.9),
            SpeakerSegment(6000, 9000, "S2", 0.9),
        ]
    )
    inference.observe_word(_DE_DOCTOR_OPENER, "S1")
    inference.observe_word(_DE_DOCTOR_QUESTION, "S1")
    for word in _DE_PATIENT_WORDS.split():
        inference.observe_word(word, "S2")

    hypothesis = inference.evaluate()
    assert hypothesis is not None
    assert hypothesis.mapping == {"S1": "doctor", "S2": "patient"}
    assert hypothesis.confidence >= 0.6


def test_german_abstains_without_clinician_register() -> None:
    """No discriminating vocabulary → no doctor/patient claim, exactly as
    for uk/en. The opener prior must never decide alone."""
    inference = SpeakerMappingInference(language="de")
    inference.observe_segments([SpeakerSegment(0, 8000, "S1", 0.9)])
    for speaker in ("S1", "S2"):
        for _ in range(8):
            inference.observe_word(_DE_PLAIN, speaker)

    assert inference.evaluate() is None
    assert inference.current is None
