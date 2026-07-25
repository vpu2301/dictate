"""Labeled TP/FP corpus for the voice-command matcher.

**As-built note (sprint 13 step 07).** The plan referred to "the
sprint-05 TP/FP corpus with precision targets". No such corpus exists:
sprint 05 shipped an 11-test *gate* suite
(`tests/unit/test_voice_command_matcher.py`) exercising pause,
confidence, edit-distance and ambiguity individually — valuable, but it
reports no precision number, so "targets still hold" had nothing to
measure against.

This file is that corpus, built at step 07 so the seeding of four new
command specs can be shown not to degrade matching. It is measured
twice in `test_command_corpus.py`: once with the sprint-05 catalogue
alone (the BASELINE) and once with the anamnesis commands added
(the AFTER). Both must hit 100% on these cases; the point is that the
two runs are compared, so any future spec that starts eating clinical
prose fails loudly.

`POSITIVES` — an utterance that MUST produce the given intent.
`NEGATIVES` — clinical prose that must produce NO command at all. The
negatives are the ones that matter: a false positive silently deletes
words from a clinical note.
"""

from __future__ import annotations

# (spoken words, expected intent or None, why)
POSITIVES: list[tuple[list[str], str, str]] = [
    # ── sprint-05 baseline commands ────────────────────────────────
    (["новий", "абзац"], "newparagraph", "canonical two-word command"),
    (["крапка"], "period", "single-word punctuation"),
    (["кома"], "comma", "single-word punctuation"),
    (["знак", "питання"], "question_mark", "two-word punctuation"),
    (["зберегти", "чернетку"], "save_draft", "editor command"),
    (["скасувати", "останнє"], "undo_last", "editor command"),
    (["кінець", "диктування"], "stop_dictation", "session command"),
    (["тире"], "dash", "single-word punctuation"),
    (["двокрапка"], "colon", "single-word punctuation"),
    (["новий", "рядок"], "newline", "two-word command"),
    # ── sprint-13 anamnesis commands ───────────────────────────────
    (["обрати", "курить"], "choice.set", "set by alias"),
    (["вибрати", "курить"], "choice.set", "alternate head"),
    (["встановити", "не", "палить"], "choice.set", "multi-word option"),
    (["обрати", "не", "палить"], "choice.set", "negated option name"),
    (["додати", "пеніцилін"], "choice.add", "multi_choice add"),
    (["прибрати", "пеніцилін"], "choice.remove", "multi_choice remove"),
    (["видалити", "латекс"], "choice.remove", "alternate remove head"),
    (["діагноз"], "diagnosis.capture", "capture hint"),
]

# Clinical prose that must NEVER trigger a command. Several deliberately
# contain command HEADS in ordinary Ukrainian usage — that is the whole
# point: "обрати" is a perfectly normal verb.
NEGATIVES: list[tuple[list[str], str]] = [
    (["пацієнту", "складно", "обрати", "зручну", "позу"], "'обрати' as ordinary verb"),
    (["важко", "вибрати", "оптимальну", "терапію"], "'вибрати' as ordinary verb"),
    (["рекомендовано", "додати", "калій", "до", "раціону"], "'додати' as ordinary verb"),
    (["слід", "прибрати", "надмірне", "навантаження"], "'прибрати' as ordinary verb"),
    (["потрібно", "видалити", "поліп"], "'видалити' as clinical action"),
    (["встановити", "діагноз", "поки", "неможливо"], "command heads inside prose"),
    (["скарги", "на", "головний", "біль"], "plain clinical prose"),
    (["артеріальний", "тиск", "стабільний"], "plain clinical prose"),
    (["пацієнт", "курить", "багато", "років"], "option name without a command head"),
    (["алергія", "на", "пеніцилін"], "option name without a command head"),
    (["призначено", "новий", "препарат"], "'новий' without its command partner"),
    (["огляд", "без", "особливостей"], "plain clinical prose"),
]
