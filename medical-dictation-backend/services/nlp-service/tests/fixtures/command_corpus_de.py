"""Labeled TP/FP corpus for the GERMAN voice-command catalogue.

Same shape and same purpose as ``command_corpus_uk``: the negatives are
the ones that matter, because a false positive silently deletes words
from a clinical note.

German raises the false-positive risk in a specific way — several
command heads are ordinary, high-frequency words ("Punkt", "Komma",
"Diagnose", "entfernen", "speichern"), and German inflection puts near
neighbours one edit away ("Punkte", "Punkten"). The catalogue answers
that with ``exact_match_only`` on the short heads; these negatives are
what proves it.

``POSITIVES`` — an utterance that MUST produce the given intent.
``NEGATIVES`` — clinical prose that must produce NO command at all.
"""

from __future__ import annotations

# (spoken words, expected intent, why)
POSITIVES: list[tuple[list[str], str, str]] = [
    (["neuer", "absatz"], "newparagraph", "canonical two-word command"),
    (["neue", "zeile"], "newline", "two-word command"),
    (["punkt"], "period", "single-word punctuation"),
    (["komma"], "comma", "single-word punctuation"),
    (["fragezeichen"], "question_mark", "single-word punctuation"),
    (["doppelpunkt"], "colon", "single-word punctuation"),
    (["gedankenstrich"], "dash", "single-word punctuation"),
    (["klammer", "auf"], "open_paren", "two-word punctuation"),
    (["klammer", "zu"], "close_paren", "two-word punctuation"),
    (["entwurf", "speichern"], "save_draft", "editor command"),
    (["rückgängig", "machen"], "undo_last", "editor command"),
    (["diktat", "beenden"], "stop_dictation", "session command"),
    (["vorlage", "einfügen"], "insert_template", "editor command"),
    (["auswählen", "raucher"], "choice.set", "option command by head + option"),
    (["hinzufügen", "penicillin"], "choice.add", "multi_choice add"),
    (["entfernen", "penicillin"], "choice.remove", "multi_choice remove"),
]

NEGATIVES: list[tuple[list[str], str]] = [
    (["an", "diesem", "punkt", "der", "untersuchung"], "'Punkt' as an ordinary noun in prose"),
    (["die", "punkte", "sind", "gerötet"], "inflected near-neighbour of the 'punkt' head"),
    (["patient", "im", "koma"], "'Koma' is one edit from the 'komma' head"),
    (["befund", "unauffällig", "und", "gesund"], "plain clinical prose"),
    (["blutdruck", "stabil", "unter", "therapie"], "plain clinical prose"),
    (["der", "patient", "raucht", "seit", "jahren"], "option name without a command head"),
    (["allergie", "gegen", "penicillin"], "option name without a command head"),
    (["neuer", "befund", "erhoben"], "'neuer' without its command partner"),
    (["wir", "müssen", "den", "polypen", "entfernen"], "'entfernen' as a clinical action"),
    (["die", "diagnose", "bleibt", "offen"], "'Diagnose' inside prose"),
    (["bitte", "die", "werte", "speichern"], "'speichern' as an instruction, not a command"),
    (["untersuchung", "ohne", "besonderheiten"], "plain clinical prose"),
]
