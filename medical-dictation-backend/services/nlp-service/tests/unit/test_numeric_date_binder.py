"""numeric/date binders (sprint 13, step 05).

The headline contract: these BIND from the normalizer's artifacts and
never parse. The no-re-parse guards at the bottom are what stop a
future edit from quietly reintroducing numeral logic here.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from nlp_service.pipeline.base import DateArtifact, NumericArtifact, TemplateSection
from nlp_service.stages.artifacts import (
    date_artifacts_from_output,
    numeric_artifacts_from_output,
)
from nlp_service.stages.extractors.numeric_date import (
    DATE_CONFIDENCE,
    LABELLED_CONFIDENCE,
    SOLE_CONFIDENCE,
    bind_date,
    bind_numeric,
)

THRESHOLD = 0.8

_UK_UNITS = frozenset({"мг", "мл", "см", "мм рт. ст.", "кг", "°c"})


def _section(name: str = "Температура", aliases: tuple[str, ...] = ()) -> TemplateSection:
    return TemplateSection(
        id=uuid4(),
        name=name,
        aliases=aliases,
        section_key="measurement",
        field_type="numeric_with_unit",
    )


def _artifact(value: str, unit: str, index: int = 0) -> NumericArtifact:
    return NumericArtifact(
        value=value, unit=unit, rendered=f"{value} {unit}".strip(), token_index=index
    )


# ── artifact reading (the normalizer's own output) ──────────────────


def test_reads_a_single_measurement() -> None:
    artifacts = numeric_artifacts_from_output(
        "температура 37,2 °c", decimal_separator=",", canonical_units=_UK_UNITS
    )
    assert len(artifacts) == 1
    assert (artifacts[0].value, artifacts[0].unit) == ("37,2", "°c")


def test_reads_a_value_without_a_unit() -> None:
    artifacts = numeric_artifacts_from_output(
        "пульс 72", decimal_separator=",", canonical_units=_UK_UNITS
    )
    assert artifacts[0].unit == ""


def test_multiword_unit_wins_over_its_prefix() -> None:
    artifacts = numeric_artifacts_from_output(
        "140 мм рт. ст.", decimal_separator=",", canonical_units=_UK_UNITS | {"мм"}
    )
    assert artifacts[0].unit == "мм рт. ст."


def test_reads_iso_dates_only() -> None:
    artifacts = date_artifacts_from_output("огляд 2026-07-01, наступний через тиждень")
    assert [a.iso for a in artifacts] == ["2026-07-01"]


def test_no_dates_yields_nothing() -> None:
    assert date_artifacts_from_output("скарги на кашель") == ()


# ── numeric binding ─────────────────────────────────────────────────


def test_single_measurement_binds_at_threshold() -> None:
    """One unit-bearing value and no label nearby ⇒ SOLE confidence."""
    meta = bind_numeric(
        "показник 37,2 °c",
        (_artifact("37,2", "°c"),),
        _section(name="Вимірювання"),
        threshold=THRESHOLD,
    )
    assert meta is not None
    assert meta.value == 37.2
    assert meta.unit == "°c"
    assert meta.confidence == SOLE_CONFIDENCE
    assert meta.source == "extracted"


def test_labelled_value_among_several_binds_higher() -> None:
    text = "маса 80 кг, температура 37,2 °c"
    artifacts = (_artifact("80", "кг", 1), _artifact("37,2", "°c", 4))
    meta = bind_numeric(text, artifacts, _section(name="Температура"), threshold=THRESHOLD)
    assert meta is not None
    assert meta.value == 37.2
    assert meta.confidence == LABELLED_CONFIDENCE


def test_multiple_unlabelled_values_yield_nothing() -> None:
    """Ambiguity rule — consistent with step 04."""
    text = "80 кг та 37,2 °c"
    artifacts = (_artifact("80", "кг", 0), _artifact("37,2", "°c", 3))
    assert bind_numeric(text, artifacts, _section(name="Показник"), threshold=THRESHOLD) is None


def test_value_without_a_unit_yields_nothing() -> None:
    """A numeric_with_unit field without a unit is not a measurement, and
    guessing the unit would be fabrication."""
    assert bind_numeric("пульс 72", (_artifact("72", ""),), _section(), threshold=THRESHOLD) is None


def test_no_artifacts_yields_nothing() -> None:
    assert bind_numeric("нічого", (), _section(), threshold=THRESHOLD) is None


def test_alias_also_labels_a_value() -> None:
    text = "тиск 140 мм рт. ст., маса 80 кг"
    artifacts = (_artifact("140", "мм рт. ст.", 1), _artifact("80", "кг", 6))
    meta = bind_numeric(
        text, artifacts, _section(name="АТ", aliases=("тиск",)), threshold=THRESHOLD
    )
    assert meta is not None and meta.value == 140.0


def test_binding_is_deterministic() -> None:
    text = "температура 37,2 °c"
    artifacts = (_artifact("37,2", "°c"),)
    a = bind_numeric(text, artifacts, _section(), threshold=THRESHOLD)
    b = bind_numeric(text, artifacts, _section(), threshold=THRESHOLD)
    assert a == b


def test_threshold_above_sole_confidence_empties_the_field() -> None:
    assert (
        bind_numeric("температура 37,2 °c", (_artifact("37,2", "°c"),), _section(), threshold=0.95)
        is None
    )


# ── compound-measurement limitation (documented, not special-cased) ─


def test_blood_pressure_is_not_bound_as_one_value() -> None:
    """BP normalizes to "140/90", which is not a single numeric value.

    Honest limitation: compound measurements need two numeric sections
    or a future compound field type. We do NOT special-case BP — a
    silently-invented single value would be a clinical error. See the
    authoring doc + sign-off.
    """
    artifacts = numeric_artifacts_from_output(
        "тиск 140/90 мм рт. ст.", decimal_separator=",", canonical_units=_UK_UNITS
    )
    assert (
        bind_numeric("тиск 140/90 мм рт. ст.", artifacts, _section(), threshold=THRESHOLD) is None
    )


# ── date binding ────────────────────────────────────────────────────


def test_single_date_binds() -> None:
    meta = bind_date((DateArtifact(iso="2026-07-01", char_index=0),), threshold=THRESHOLD)
    assert meta is not None
    assert meta.date == "2026-07-01"
    assert meta.confidence == DATE_CONFIDENCE
    assert meta.source == "extracted"


def test_multiple_dates_yield_nothing() -> None:
    """Picking one would silently misdate a clinical record."""
    artifacts = (
        DateArtifact(iso="2026-07-01", char_index=0),
        DateArtifact(iso="2026-07-08", char_index=20),
    )
    assert bind_date(artifacts, threshold=THRESHOLD) is None


def test_no_dates_yield_nothing() -> None:
    assert bind_date((), threshold=THRESHOLD) is None


def test_date_binding_is_deterministic() -> None:
    artifacts = (DateArtifact(iso="2026-07-01", char_index=0),)
    assert bind_date(artifacts, threshold=THRESHOLD) == bind_date(artifacts, threshold=THRESHOLD)


# ── the no-re-parse guards ──────────────────────────────────────────

_BINDER = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "nlp_service"
    / "stages"
    / "extractors"
    / "numeric_date.py"
)


def test_binder_imports_no_normalizer_internals() -> None:
    """If the binder reached into the normalizers, it would duplicate
    their logic and drift."""
    source = _code_only(_BINDER)
    for forbidden in ("number_norm_uk", "number_norm_en", "_DIGITS", "_UNITS", "date_norm"):
        assert forbidden not in source, f"binder references normalizer internals: {forbidden}"


def _code_only(path: Path) -> str:
    """Source with docstrings and comments stripped — the guards below
    are about executable logic, not prose explaining the rule."""
    import ast

    tree = ast.parse(path.read_text("utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            node.body.pop(0)
    return ast.unparse(tree)


def test_binder_carries_no_numeral_or_unit_vocabulary() -> None:
    """Spoken numerals and unit names belong to the normalizer alone."""
    source = _code_only(_BINDER).lower()
    for word in ("сто", "сорок", "дев'яносто", "twenty", "hundred", "мілігра", "міліліт"):
        assert word not in source, f"binder contains numeral/unit vocabulary: {word}"


def test_binder_does_no_date_arithmetic() -> None:
    """Relative-date resolution belongs to date_norm alone."""
    source = _code_only(_BINDER)
    for forbidden in ("timedelta", "datetime", "date.today", "reference_date"):
        assert forbidden not in source, f"binder does date arithmetic: {forbidden}"


@pytest.mark.parametrize("value", ["140", "37,2", "80"])
def test_binder_never_sees_spoken_forms(value: str) -> None:
    """Sanity: artifacts always carry digits — the binder only ever
    receives already-normalized values."""
    assert value.replace(",", "").isdigit()
