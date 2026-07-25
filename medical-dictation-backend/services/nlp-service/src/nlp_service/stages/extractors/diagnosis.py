"""structured_diagnosis → ICD-10 PROPOSALS (sprint 13, step 05).

**This module cannot select a code.** It emits `DiagnosisMeta`, whose
`proposals` are staging data; confirmed codes live in
`ReportSection.icd10` and get there only through the clinician's
explicit confirm in report-service. A wrong ICD-10 is a clinical and
billing error, so auto-selection is prevented by construction, not by
discipline — nlp-service has no write path to `section.icd10` at all
(enforced by a grep test).

Candidate extraction is deliberately conservative. Over-splitting a
dictated sentence fabricates diagnoses that were never said, so when
the split is not obvious the whole string stays one candidate.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Final, Protocol

from report_models import DiagnosisMeta, DiagnosisProposal

logger = logging.getLogger(__name__)

# Preamble the clinician says before naming the diagnosis; it carries no
# diagnostic content and would only dilute the match score.
_PREAMBLE: Final[frozenset[str]] = frozenset(
    {
        "діагноз",
        "діагнози",
        "основний",
        "основне",
        "клінічний",
        "встановлено",
        "встановлений",
        "виставлено",
        "захворювання",
        "diagnosis",
        "primary",
        "established",
    }
)

# Conservative split points only. Bare "і"/"та" are NOT split points:
# "ішемічна хвороба серця і гіпертонічна хвороба" splits correctly, but
# "нудота і блювання" is ONE diagnosis (R11) — and we cannot tell them
# apart without parsing, so we never split on them.
_SPLIT_RE: Final = re.compile(r"\s*[;,]\s*|\s+\+\s+")

MAX_PROPOSALS_PER_CANDIDATE: Final = 3
MAX_PROPOSALS_PER_SECTION: Final = 5
LOOKUP_LIMIT: Final = 5
# Rank decay: the lookup's 1st hit keeps its string score, the 2nd loses
# 10%, and so on. Keeps a weak-but-top hit from outranking a strong 3rd.
RANK_DECAY: Final = 0.9
# A one-word candidate is weak evidence of a diagnosis: "гіпертонічна"
# alone covers itself perfectly but says much less than "гіпертонічна
# хвороба". Without this damping a single word would score 1.0 and
# propose at any threshold.
SINGLE_TOKEN_DAMPING: Final = 0.75


class Icd10Lookup(Protocol):
    """The step-03 repository surface this extractor needs."""

    async def __call__(self, *, query: str, limit: int) -> list[object]: ...  # pragma: no cover


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text).lower()


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^0-9a-zа-яїієґё']+", _normalize(text)) if t]


def candidates(text: str) -> list[str]:
    """Split a dictated diagnosis section into candidate strings.

    Conservative by design: only explicit delimiters split. Preamble
    words are stripped from each candidate, and a candidate that is
    nothing but preamble is dropped.
    """
    out: list[str] = []
    for chunk in _SPLIT_RE.split(text):
        kept = [t for t in _tokens(chunk) if t not in _PREAMBLE]
        if kept:
            out.append(" ".join(kept))
    return out


def token_set_score(candidate: str, display: str) -> float:
    """How much of what the clinician SAID this code's title covers, [0, 1].

    Coverage of the candidate — not symmetric overlap. ICD-10 display
    titles are long formal names ("Гіпертонічна хвороба з переважним
    ураженням серця із серцевою недостатністю") while clinicians dictate
    short ones ("гіпертонічна хвороба"). A symmetric measure divides by
    the long title and puts every realistic utterance below any sane
    threshold, i.e. the extractor would propose nothing, ever.

    Ordering *within* a matched family comes from the lookup's own rank
    (see ``score_hit``), not from title length — a more elaborate title
    is not a worse match, just a more specific one, and choosing among
    them is exactly the clinician's job.

    Deliberately a local, exact computation rather than a fuzzy-matching
    library call: this feeds a clinical proposal's confidence, and the
    stage's output is frozen by ``pipeline_version`` (see ADR-0032 on
    why a library version must not be able to move these numbers).
    """
    a, b = set(_tokens(candidate)), set(_tokens(display))
    if not a or not b:
        return 0.0
    coverage = len(a & b) / len(a)
    if len(a) == 1:
        coverage *= SINGLE_TOKEN_DAMPING
    return round(coverage, 6)


def score_hit(candidate: str, display: str, rank_index: int) -> float:
    return round(token_set_score(candidate, display) * (RANK_DECAY**rank_index), 6)


async def extract_diagnosis(
    text: str,
    *,
    lookup: Icd10Lookup,
    threshold: float,
) -> DiagnosisMeta | None:
    """Ranked ICD-10 proposals, or ``None`` for no metadata at all.

    Fail-EMPTY (not fail-closed): if the reference lookup times out or
    errors, this returns ``None`` and the pipeline completes normally.
    Extraction is an enhancement; a hiccuping reference table must never
    fail a clinician's dictation. (Contrast the sprint-11 consent gate,
    which is correctly fail-CLOSED — that one protects a patient right;
    this one offers a convenience.)
    """
    proposals: list[DiagnosisProposal] = []
    seen: set[str] = set()

    for candidate in candidates(text):
        try:
            hits = await lookup(query=candidate, limit=LOOKUP_LIMIT)
        except Exception as exc:  # noqa: BLE001 — fail-empty by design
            logger.warning(
                "diagnosis_extraction.lookup_failed",
                extra={"error_class": type(exc).__name__, "error": str(exc)},
            )
            return None

        scored: list[DiagnosisProposal] = []
        for index, hit in enumerate(hits):
            code = getattr(hit, "code", None)
            display = getattr(hit, "display_uk", "")
            if not code or code in seen:
                continue
            confidence = score_hit(candidate, display, index)
            if confidence < threshold:
                continue
            scored.append(
                DiagnosisProposal(code=code, display=display or None, confidence=confidence)
            )
        # Deterministic: strongest first, code ascending on ties.
        scored.sort(key=lambda p: (-p.confidence, p.code))
        for proposal in scored[:MAX_PROPOSALS_PER_CANDIDATE]:
            if proposal.code not in seen:
                seen.add(proposal.code)
                proposals.append(proposal)

    if not proposals:
        return None

    proposals.sort(key=lambda p: (-p.confidence, p.code))
    capped = tuple(proposals[:MAX_PROPOSALS_PER_SECTION])
    return DiagnosisMeta(
        proposals=capped,
        # The section-level confidence is the best proposal's — the
        # metadata describes how sure we are that ANY of these is right.
        confidence=capped[0].confidence,
        source="extracted",
    )
