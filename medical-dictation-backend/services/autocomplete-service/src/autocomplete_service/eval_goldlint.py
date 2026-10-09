"""The gold-transcript linter — corpus-v3 Epic B.

THE RULE IT ENFORCES (eval/corpus/README.md): a gold transcript is written
in SPOKEN form. Numbers as words, no digits, no "/" and no "%", and
abbreviations spelled the way a clinician actually says them.

WHY THE RULE, WHEN THE NORMALISER EXISTS. Corpus-v2 measured 18.1% raw
against 14.4% normalised, and the gap was not recognition error — it was
house style. "пульс 68/хв" in the gold against "пульс шістдесят вісім за
хвилину" from the model is a 50% WER on a line the ASR got completely right.
The normaliser closes that gap for the normalised number, and the normalised
number alone is not enough: it is the score that can be tuned into
meaninglessness by adding rules, so the raw score has to stay honest too.
The normaliser is a safety net, not a licence for careless references.

WHY WARNINGS AND NOT REJECTIONS. A gold with a digit in it is usually wrong
and occasionally right — "HbA1c" and "B12" are names, not measurements, and
a linter that refused them would be refusing correct data. So every finding
is advisory, and the useful half of the tool is the SUGGESTION: the reverse
normaliser proposes the spoken rewrite and the author accepts it with one
click. A rule nobody can comply with cheaply is a rule that gets ignored.

THE SUGGESTION IS VERIFIED BEFORE IT IS OFFERED. ``eval_spoken`` gets
grammar approximately right and the number exactly right, but "approximately"
is not a property one asserts by hope: every proposal is normalised back and
compared against the original's canonical form and numeric signature. A
proposal that changed the dose is discarded rather than shown. That is why
``suggestion`` can be None on a line that clearly has a finding — the
linter would rather say "this needs your attention" than guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from . import eval_normalize, eval_spoken

__all__ = ["Finding", "LintResult", "lint", "lint_gold"]

#: A digit anywhere. Deliberately blunt: the suppression of legitimate
#: identifiers happens later, by noticing the rewriter left them alone.
_DIGIT: Final = re.compile(r"\d")
_SLASH: Final = re.compile(r"/")
_PERCENT: Final = re.compile(r"%")
_DOUBLE_SPACE: Final = re.compile(r"  +")
_WHITESPACE: Final = re.compile(r"\s+")

#: Codes the console and the CSV importer render. Kept short and stable —
#: they are part of the API surface, and the SPA maps them to Ukrainian text.
CODES: Final[tuple[str, ...]] = (
    "digits_in_gold",
    "slash_in_gold",
    "percent_in_gold",
    "double_space",
    "edge_whitespace",
)


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    #: The offending fragment, for a console that highlights it in place.
    detail: str


@dataclass(frozen=True, slots=True)
class LintResult:
    findings: tuple[Finding, ...]
    #: The proposed spoken rewrite, or None when nothing safe can be offered.
    suggestion: str | None

    @property
    def clean(self) -> bool:
        return not self.findings

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(f.code for f in self.findings)

    def as_dict(self) -> dict[str, object]:
        return {
            "findings": [{"code": f.code, "detail": f.detail} for f in self.findings],
            "suggestion": self.suggestion,
        }


def _tidy(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def _propose(text: str, language: str) -> str | None:
    """The spoken rewrite, or None if it would not be an improvement.

    Three ways a proposal is refused, in order of how much they matter:

      1. it does not mean the same thing — the canonical form or the numeric
         signature moved, which for a dose is a clinical-safety bug;
      2. it is identical to the input, so there is nothing to accept;
      3. it is not an improvement by the linter's own measure.

    Rule 3 is a comparison and not "the rewrite must be clean", because a
    partial fix is still worth offering. "HbA1c 6,9" becomes "HbA1c шість
    цілих дев'ять десятих": the assay name keeps its digit — it is a name —
    and the measurement stops being written in digits, which is the point.
    Insisting the result be spotless would throw that suggestion away and
    leave the author with a complaint and no remedy.
    """
    rewritten = _tidy(eval_spoken.to_spoken(text, language))
    if rewritten == text:
        return None
    same_reading = eval_normalize.normalize(rewritten, language) == (
        eval_normalize.normalize(text, language)
    )
    same_numbers = eval_normalize.numeric_signature(rewritten, language) == (
        eval_normalize.numeric_signature(text, language)
    )
    if not (same_reading and same_numbers):
        return None
    if _severity(rewritten) > _severity(text):
        return None
    return rewritten


def _severity(text: str) -> int:
    """How far this text is from the style guide, as a number to compare.

    Counts written-form characters rather than findings so that fixing three
    of four numbers registers as progress — a per-code verdict would call
    that "still has digits" and refuse the whole suggestion.
    """
    return (
        len(_DIGIT.findall(text))
        + len(_SLASH.findall(text))
        + len(_PERCENT.findall(text))
        + len(_DOUBLE_SPACE.findall(text))
        + (1 if text != text.strip() else 0)
    )


def _raw_findings(text: str) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    digits = _DIGIT.findall(text)
    if digits:
        # Report the words the digits live in, not the digits: "пульс 68/хв"
        # is what the author has to look at, not "6", "8".
        fragments = [w for w in text.split() if _DIGIT.search(w)]
        findings.append(Finding("digits_in_gold", " ".join(fragments)))
    if _SLASH.search(text):
        findings.append(
            Finding("slash_in_gold", " ".join(w for w in text.split() if "/" in w))
        )
    if _PERCENT.search(text):
        findings.append(Finding("percent_in_gold", "%"))
    if _DOUBLE_SPACE.search(text):
        findings.append(Finding("double_space", ""))
    if text != text.strip():
        findings.append(Finding("edge_whitespace", ""))
    return tuple(findings)


def lint(text: str, language: str) -> LintResult:
    """Findings plus a verified suggestion for one gold transcript."""
    if not text:
        return LintResult(findings=(), suggestion=None)
    findings = _raw_findings(text)
    if not findings:
        return LintResult(findings=(), suggestion=None)
    return LintResult(findings=findings, suggestion=_propose(text, language))


def lint_gold(
    *, say: str, transcript: str | None, language: str
) -> LintResult:
    """Lint the text that will actually be scored.

    A line whose gold is not given is scored against its spoken form, which
    is the style guide's own answer — so there is nothing to complain about
    and, more importantly, nothing to propose changing.
    """
    if transcript is None or _tidy(transcript) == _tidy(say):
        return LintResult(findings=(), suggestion=None)
    return lint(transcript, language)
