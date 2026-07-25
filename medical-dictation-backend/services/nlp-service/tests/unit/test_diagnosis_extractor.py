"""structured_diagnosis → ICD-10 proposals (sprint 13, step 05).

The invariant under test everywhere here: this extractor can PROPOSE
but never SELECT. A wrong ICD-10 is a clinical and billing error.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from nlp_service.stages.extractors.diagnosis import (
    MAX_PROPOSALS_PER_SECTION,
    candidates,
    extract_diagnosis,
    score_hit,
    token_set_score,
)

pytestmark = pytest.mark.asyncio

THRESHOLD = 0.8


class _Hit:
    """Mirrors ``icd10_repository.Icd10Hit``'s duck type."""

    def __init__(self, code: str, display_uk: str) -> None:
        self.code = code
        self.display_uk = display_uk


# A slice of the committed step-03 fixture table.
_FIXTURE = {
    "гіпертонічна хвороба": [
        _Hit(
            "I11.0", "Гіпертонічна хвороба з переважним ураженням серця із серцевою недостатністю"
        ),
        _Hit(
            "I11.9", "Гіпертонічна хвороба з переважним ураженням серця без серцевої недостатності"
        ),
        _Hit("I12.0", "Гіпертонічна хвороба з ураженням нирок із нирковою недостатністю"),
    ],
    "цукровий діабет 2 типу": [
        _Hit("E11.9", "Цукровий діабет 2 типу без ускладнень"),
        _Hit("E11.0", "Цукровий діабет 2 типу з комою"),
        _Hit("E11.2", "Цукровий діабет 2 типу з ураженням нирок"),
    ],
    "астма": [
        _Hit("J45.9", "Астма, неуточнена"),
        _Hit("J45.0", "Астма з переважанням алергічного компонента"),
    ],
}


def _lookup(table: dict[str, list[_Hit]] = _FIXTURE):
    async def _call(*, query: str, limit: int) -> list[_Hit]:
        for key, hits in table.items():
            if key in query or query in key:
                return hits[:limit]
        return []

    return _call


# ── candidate extraction (conservative by design) ───────────────────


async def test_preamble_is_stripped() -> None:
    assert candidates("Діагноз: гіпертонічна хвороба") == ["гіпертонічна хвороба"]


async def test_explicit_delimiters_split() -> None:
    assert candidates("гіпертонічна хвороба; цукровий діабет") == [
        "гіпертонічна хвороба",
        "цукровий діабет",
    ]


async def test_bare_conjunctions_never_split() -> None:
    """ "нудота і блювання" is ONE diagnosis (R11). We cannot tell it from
    a two-diagnosis sentence without parsing, so we never split on
    bare "і"/"та" — over-splitting fabricates diagnoses."""
    assert candidates("нудота і блювання") == ["нудота і блювання"]
    assert candidates("ішемічна хвороба серця та гіпертонічна хвороба") == [
        "ішемічна хвороба серця та гіпертонічна хвороба"
    ]


async def test_preamble_only_text_yields_no_candidates() -> None:
    assert candidates("Діагноз встановлено") == []


async def test_empty_text_yields_no_candidates() -> None:
    assert candidates("") == []


# ── scoring ─────────────────────────────────────────────────────────


async def test_single_token_candidate_is_damped() -> None:
    """One word is weak evidence of a diagnosis, even matched exactly."""
    assert token_set_score("астма", "Астма") == 0.75


async def test_multi_token_exact_match_scores_one() -> None:
    assert token_set_score("гострий бронхіт", "Гострий бронхіт") == 1.0


async def test_full_coverage_of_a_long_title_scores_one() -> None:
    """A clinician's short phrase fully covered by a long formal title is
    a strong match — the title's extra words are specificity, not noise."""
    assert token_set_score("гіпертонічна хвороба", "Гіпертонічна хвороба з ураженням нирок") == 1.0


async def test_partial_coverage_scores_between() -> None:
    score = token_set_score("гіпертонічна хвороба нирок", "Гіпертонічна хвороба")
    assert 0.0 < score < 1.0


async def test_no_overlap_scores_zero() -> None:
    assert token_set_score("астма", "Перелом стегнової кістки") == 0.0


async def test_rank_decay_penalises_later_hits() -> None:
    assert score_hit("астма", "Астма", 0) > score_hit("астма", "Астма", 1)


# ── the VERIFY pair ─────────────────────────────────────────────────


async def test_hypertension_yields_i10_family_proposals() -> None:
    meta = await extract_diagnosis("Діагноз: гіпертонічна хвороба", lookup=_lookup(), threshold=0.4)
    assert meta is not None
    codes = [p.code for p in meta.proposals]
    assert codes, "hypertension must propose something"
    assert all(c.startswith("I1") for c in codes), codes
    assert meta.source == "extracted"


async def test_below_threshold_proposes_nothing() -> None:
    """The prime directive in one assertion."""
    meta = await extract_diagnosis(
        "щось незрозуміле бурмотіння", lookup=_lookup(), threshold=THRESHOLD
    )
    assert meta is None


async def test_high_threshold_suppresses_weak_proposals() -> None:
    assert await extract_diagnosis("гіпертонічна", lookup=_lookup(), threshold=0.99) is None


async def test_diabetes_yields_e11_family() -> None:
    meta = await extract_diagnosis("цукровий діабет 2 типу", lookup=_lookup(), threshold=0.5)
    assert meta is not None
    assert all(p.code.startswith("E11") for p in meta.proposals)


# ── proposal shape + caps ───────────────────────────────────────────


async def test_proposals_are_ordered_strongest_first() -> None:
    meta = await extract_diagnosis("астма", lookup=_lookup(), threshold=0.1)
    assert meta is not None
    confidences = [p.confidence for p in meta.proposals]
    assert confidences == sorted(confidences, reverse=True)


async def test_section_cap_is_enforced() -> None:
    many = [_Hit(f"J{40 + i}.9", "Астма") for i in range(8)]
    meta = await extract_diagnosis("астма", lookup=_lookup({"астма": many}), threshold=0.1)
    assert meta is not None
    assert len(meta.proposals) <= MAX_PROPOSALS_PER_SECTION


async def test_duplicate_codes_are_deduped() -> None:
    dupes = [_Hit("J45.9", "Астма"), _Hit("J45.9", "Астма")]
    meta = await extract_diagnosis("астма", lookup=_lookup({"астма": dupes}), threshold=0.1)
    assert meta is not None
    assert len({p.code for p in meta.proposals}) == len(meta.proposals)


async def test_section_confidence_is_the_best_proposal() -> None:
    meta = await extract_diagnosis("астма", lookup=_lookup(), threshold=0.1)
    assert meta is not None
    assert meta.confidence == max(p.confidence for p in meta.proposals)


# ── fail-empty ──────────────────────────────────────────────────────


async def test_lookup_timeout_yields_nothing_and_does_not_raise() -> None:
    """Fail-EMPTY, not fail-closed: a hiccuping reference table must
    never fail a clinician's dictation."""

    async def _timeout(*, query: str, limit: int) -> list[_Hit]:
        raise TimeoutError("db slow")

    assert await extract_diagnosis("астма", lookup=_timeout, threshold=0.1) is None


async def test_lookup_error_yields_nothing() -> None:
    async def _boom(*, query: str, limit: int) -> list[_Hit]:
        raise RuntimeError("connection reset")

    assert await extract_diagnosis("астма", lookup=_boom, threshold=0.1) is None


async def test_empty_lookup_result_yields_nothing() -> None:
    async def _none(*, query: str, limit: int) -> list[_Hit]:
        return []

    assert await extract_diagnosis("астма", lookup=_none, threshold=0.1) is None


# ── determinism ─────────────────────────────────────────────────────


async def test_double_run_is_identical() -> None:
    a = await extract_diagnosis("гіпертонічна хвороба", lookup=_lookup(), threshold=0.4)
    b = await extract_diagnosis("гіпертонічна хвороба", lookup=_lookup(), threshold=0.4)
    assert a == b


# ── auto-selection is impossible ────────────────────────────────────

_SRC = Path(__file__).resolve().parents[2] / "src" / "nlp_service"


async def test_proposals_are_never_marked_manual() -> None:
    meta = await extract_diagnosis("астма", lookup=_lookup(), threshold=0.1)
    assert meta is not None
    assert meta.source == "extracted"


async def test_nlp_service_has_no_write_path_to_section_icd10() -> None:
    """Confirmed codes live in ReportSection.icd10 and get there only via
    the clinician's confirm in report-service. nlp-service must have no
    way to write them at all."""
    offenders: list[str] = []
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            # An assignment to anything named `icd10` / `icd10_codes`.
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                name = getattr(target, "attr", None) or getattr(target, "id", None)
                if name in {"icd10", "icd10_codes"}:
                    offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], f"nlp-service writes section.icd10 at {offenders}"


async def test_nlp_service_never_imports_report_section() -> None:
    """The content model's section type is report-service's to write."""
    for path in _SRC.rglob("*.py"):
        source = path.read_text("utf-8")
        assert "import ReportSection" not in source, path.name
        assert "ReportContent" not in source, path.name
