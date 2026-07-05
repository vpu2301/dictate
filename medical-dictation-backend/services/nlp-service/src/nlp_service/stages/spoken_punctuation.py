"""Deterministic spoken-punctuation normalization.

Turns spoken punctuation *commands* that the ASR transcribed as literal
words — "крапка", "кома", "period", "comma" — into the actual marks
(``.``, ``,`` …). This is the deterministic safety net that complements
the prosody-gated voice-command FSM in :mod:`.voice_commands`:

- The FSM (Stage 1) needs word-level *timing* + Whisper *probability* to
  fire, and is deliberately inert on the batch / text-only path (no word
  timings there). So spoken punctuation words survive into the transcript.
- Even on the streaming path, the FSM's pause/confidence gates err toward
  "didn't fire" (false negatives beat false positives for clinical
  trust), leaving residual command words in the text.

This module operates on the **text alone**, is language-aware, and is
conservative on ambiguous words ("dot", "period", "питання") so it never
corrupts normal prose. It is data-driven: adding a language or a command
is a table edit (see ``_RULES_BY_LANGUAGE``).

Spacing / capitalization contract (requirement from the ASR spec):
- No space *before* a mark; one space *after* it.
- A sentence terminator (``.`` ``!`` ``?``) and a line break capitalize
  the following word; ``,`` ``:`` ``;`` do not.
- Line breaks never leave a trailing/leading space or a duplicated space.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .punctuation_post import capitalize_first_letter

# ── Command catalogue ───────────────────────────────────────────────

_NEWLINE = "\n"
_PARAGRAPH = "\n\n"
_MARKS = frozenset({".", ",", "?", "!", ":", ";"})


@dataclass(frozen=True, slots=True)
class _Rule:
    """One spoken-punctuation command.

    ``phrase`` is a lower-cased word tuple; ``mark`` is what it renders to
    (a punctuation char, ``\\n`` or ``\\n\\n``). ``ambiguous`` flags a
    command whose surface form is also a normal word — it is only applied
    when :func:`_convertible` clears it.
    """

    phrase: tuple[str, ...]
    mark: str
    ambiguous: bool = False


# Ukrainian. Multi-word phrases are ranked longest-first at build time so
# "крапка з комою" wins over "крапка", and "знак питання" over "питання".
_UK_RULES: tuple[_Rule, ...] = (
    _Rule(("крапка", "з", "комою"), ";"),
    _Rule(("знак", "питання"), "?"),
    _Rule(("знак", "оклику"), "!"),
    _Rule(("новий", "абзац"), _PARAGRAPH),
    _Rule(("новий", "рядок"), _NEWLINE),
    _Rule(("крапка",), "."),
    _Rule(("крапку",), "."),  # accusative ("постав крапку") — unambiguous
    _Rule(("кома",), ","),
    _Rule(("двокрапка",), ":"),
    # Bare "питання" is an extremely common noun; only a *trailing*
    # occurrence is treated as "?" (see _convertible).
    _Rule(("питання",), "?", ambiguous=True),
)

# English.
_EN_RULES: tuple[_Rule, ...] = (
    _Rule(("question", "mark"), "?"),
    _Rule(("exclamation", "mark"), "!"),
    _Rule(("exclamation", "point"), "!"),
    _Rule(("full", "stop"), "."),
    _Rule(("new", "paragraph"), _PARAGRAPH),
    _Rule(("new", "line"), _NEWLINE),
    _Rule(("period",), "."),
    _Rule(("comma",), ","),
    _Rule(("colon",), ":"),
    _Rule(("semicolon",), ";"),
    _Rule(("semi", "colon"), ";"),
    # "example dot com", "three dot five" — kept unless clearly a command.
    _Rule(("dot",), ".", ambiguous=True),
)


def _index(rules: tuple[_Rule, ...]) -> tuple[_Rule, ...]:
    """Longest phrase first — greedy longest-match at each position."""
    return tuple(sorted(rules, key=lambda r: -len(r.phrase)))


_RULES_BY_LANGUAGE: dict[str, tuple[_Rule, ...]] = {
    "uk": _index(_UK_RULES),
    "en": _index(_EN_RULES),
}

# Words that make "dot" a URL/decimal reading rather than a command.
_URL_WORDS = frozenset(
    {"com", "net", "org", "ua", "io", "gov", "edu", "co", "www", "gmail"}
)

# Spelled-out English number words — a "dot" flanked by these is a decimal
# ("three dot five"), not a sentence boundary.
_NUMBER_WORDS_EN = frozenset(
    {
        "zero", "oh", "one", "two", "three", "four", "five", "six", "seven",
        "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
        "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
        "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety",
        "hundred", "thousand", "point",
    }
)


# ── Conservative context checks for ambiguous / homographic commands ─


def _is_numberish(token: str | None) -> bool:
    if token is None:
        return False
    return any(ch.isdigit() for ch in token) or token in _NUMBER_WORDS_EN


def _convertible(rule: _Rule, prev: str | None, nxt: str | None) -> bool:
    """Guard ambiguous commands against corrupting normal prose.

    ``prev`` / ``nxt`` are the normalized neighbouring tokens (``None`` at
    a boundary).
    """
    phrase = rule.phrase
    if phrase == ("dot",):
        # Protect decimals ("three dot five", "3 dot 5") and domains
        # ("example dot com").
        if _is_numberish(prev) or _is_numberish(nxt):
            return False
        return not (prev in _URL_WORDS or nxt in _URL_WORDS)
    if phrase == ("питання",):
        # Only a trailing "питання" (a clause, then "…?") is plausibly a
        # command; mid-sentence it is the noun "question". The unambiguous
        # form is "знак питання", which is matched first.
        return nxt is None and prev is not None
    if phrase == ("period",):
        # "a period of two weeks", "grace period of observation" — the
        # dominant clinical false positive is "period of".
        return nxt != "of"
    return True


# ── Tokenization ────────────────────────────────────────────────────

_STRIP_CHARS = ".,!?;:()\"'«»„“”"


def _norm(token: str) -> str:
    """Lower-case + strip surrounding punctuation for phrase matching.

    Matching-only: the original token is preserved for non-command words,
    so upstream punctuation/casing on real words is never lost.
    """
    return token.lower().strip(_STRIP_CHARS)


# ── Public API ──────────────────────────────────────────────────────


def normalize_spoken_punctuation(text: str, language: str) -> str:
    """Convert spoken punctuation commands in ``text`` to real marks.

    Deterministic and idempotent-friendly. Unknown languages and blank
    input are returned unchanged.
    """
    if not text or not text.strip():
        return text
    rules = _RULES_BY_LANGUAGE.get(language)
    if not rules:
        return text

    tokens = text.split()
    norm = [_norm(t) for t in tokens]
    n = len(tokens)

    # atoms: ("word", str) | ("mark", str) | ("newline", str)
    atoms: list[tuple[str, str]] = []
    i = 0
    while i < n:
        matched: _Rule | None = None
        for rule in rules:
            length = len(rule.phrase)
            if i + length > n:
                continue
            if tuple(norm[i : i + length]) != rule.phrase:
                continue
            prev = norm[i - 1] if i > 0 else None
            nxt = norm[i + length] if i + length < n else None
            if not _convertible(rule, prev, nxt):
                continue
            matched = rule
            break

        if matched is None:
            atoms.append(("word", tokens[i]))
            i += 1
        else:
            kind = "newline" if matched.mark in (_NEWLINE, _PARAGRAPH) else "mark"
            atoms.append((kind, matched.mark))
            i += len(matched.phrase)

    return _apply_capitalization(_render(atoms))


# ── Rendering ───────────────────────────────────────────────────────


def _render(atoms: list[tuple[str, str]]) -> str:
    """Join atoms with correct spacing (no space before a mark; one after)."""
    buf = ""
    for kind, val in atoms:
        if kind == "word":
            if buf and not buf.endswith("\n"):
                buf += " "
            buf += val
        elif kind == "mark":
            buf = buf.rstrip(" ")
            # Collapse a redundant mark an upstream punctuator may have
            # attached to the previous token ("chest pain." + explicit
            # "period" → one "."). Different mark → the explicit one wins.
            if buf and buf[-1] in _MARKS:
                buf = buf[:-1]
            buf += val
        else:  # newline
            buf = buf.rstrip(" ")
            buf += val
    return buf


_CAP_AFTER = re.compile(r"([.!?]\s+|\n+)([^\W\d_])", re.UNICODE)


def _apply_capitalization(text: str) -> str:
    """Capitalize the first word and any word after a terminator / newline.

    Whitespace runs (including newlines) in group 1 are preserved verbatim
    — only the following letter is upcased — so line/paragraph breaks are
    never collapsed to a space.
    """
    text = capitalize_first_letter(text)
    return _CAP_AFTER.sub(lambda m: m.group(1) + m.group(2).upper(), text)


__all__ = ["normalize_spoken_punctuation"]
