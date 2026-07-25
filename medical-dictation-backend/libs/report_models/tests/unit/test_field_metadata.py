"""Sprint-13 field_specific_metadata contract tests."""

from __future__ import annotations

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from report_models import (
    META_MODEL_BY_FIELD_TYPE,
    ChoiceMeta,
    DateMeta,
    DiagnosisMeta,
    FieldMetadataError,
    Icd10Code,
    MultiChoiceMeta,
    NumericMeta,
    parse_field_metadata,
    validate_field_metadata,
)

# ── choice ──────────────────────────────────────────────────────────


def test_choice_extracted_valid() -> None:
    meta = parse_field_metadata(
        "choice", {"selected": "never", "confidence": 0.92, "source": "extracted"}
    )
    assert isinstance(meta, ChoiceMeta)
    assert meta.selected == "never"


def test_choice_manual_valid_without_confidence() -> None:
    meta = parse_field_metadata("choice", {"selected": "never", "source": "manual"})
    assert isinstance(meta, ChoiceMeta)
    assert meta.confidence is None


def test_choice_extracted_requires_confidence() -> None:
    with pytest.raises(FieldMetadataError, match="requires confidence"):
        parse_field_metadata("choice", {"selected": "never", "source": "extracted"})


def test_choice_manual_forbids_confidence() -> None:
    with pytest.raises(FieldMetadataError, match="must omit confidence"):
        parse_field_metadata("choice", {"selected": "never", "confidence": 1.0, "source": "manual"})


def test_choice_missing_source_rejected() -> None:
    with pytest.raises(FieldMetadataError, match="source"):
        parse_field_metadata("choice", {"selected": "never"})


def test_choice_unknown_key_rejected() -> None:
    with pytest.raises(FieldMetadataError):
        parse_field_metadata(
            "choice",
            {"selected": "never", "source": "manual", "note": "extra"},
        )


def test_choice_confidence_out_of_range_rejected() -> None:
    with pytest.raises(FieldMetadataError):
        parse_field_metadata(
            "choice", {"selected": "never", "confidence": 1.5, "source": "extracted"}
        )


def test_choice_bad_source_rejected() -> None:
    with pytest.raises(FieldMetadataError):
        parse_field_metadata("choice", {"selected": "never", "source": "guessed"})


# ── multi_choice ────────────────────────────────────────────────────


def test_multi_choice_valid() -> None:
    meta = parse_field_metadata(
        "multi_choice",
        {"selected": ["penicillin", "latex"], "confidence": 0.85, "source": "extracted"},
    )
    assert isinstance(meta, MultiChoiceMeta)
    assert meta.selected == ("penicillin", "latex")


def test_multi_choice_empty_selection_rejected() -> None:
    # An empty selection is an empty metadata dict, not an empty list.
    with pytest.raises(FieldMetadataError):
        parse_field_metadata("multi_choice", {"selected": [], "source": "manual"})


def test_multi_choice_duplicate_values_rejected() -> None:
    with pytest.raises(FieldMetadataError, match="unique"):
        parse_field_metadata("multi_choice", {"selected": ["latex", "latex"], "source": "manual"})


def test_multi_choice_scalar_selected_rejected() -> None:
    with pytest.raises(FieldMetadataError):
        parse_field_metadata("multi_choice", {"selected": "latex", "source": "manual"})


# ── structured_diagnosis ────────────────────────────────────────────


def test_diagnosis_proposals_valid() -> None:
    meta = parse_field_metadata(
        "structured_diagnosis",
        {
            "proposals": [{"code": "I10", "display": "Гіпертензія", "confidence": 0.9}],
            "source": "extracted",
            "confidence": 0.9,
        },
    )
    assert isinstance(meta, DiagnosisMeta)
    assert meta.proposals[0].code == "I10"


def test_diagnosis_proposal_requires_confidence() -> None:
    with pytest.raises(FieldMetadataError):
        parse_field_metadata(
            "structured_diagnosis",
            {"proposals": [{"code": "I10"}], "source": "extracted", "confidence": 0.9},
        )


def test_diagnosis_bad_code_rejected() -> None:
    with pytest.raises(FieldMetadataError):
        parse_field_metadata(
            "structured_diagnosis",
            {
                "proposals": [{"code": "NOPE", "confidence": 0.9}],
                "source": "extracted",
                "confidence": 0.9,
            },
        )


def test_diagnosis_empty_proposals_rejected() -> None:
    with pytest.raises(FieldMetadataError):
        parse_field_metadata(
            "structured_diagnosis", {"proposals": [], "source": "extracted", "confidence": 0.5}
        )


def test_diagnosis_code_pattern_locksteps_icd10code() -> None:
    """DiagnosisProposal.code must accept exactly what Icd10Code.code does
    (proposals graduate into section.icd10 on confirmation)."""
    from report_models.field_metadata import DiagnosisProposal

    prop_pattern = DiagnosisProposal.model_fields["code"].metadata
    icd_pattern = Icd10Code.model_fields["code"].metadata
    prop_re = next(m.pattern for m in prop_pattern if hasattr(m, "pattern"))
    icd_re = next(m.pattern for m in icd_pattern if hasattr(m, "pattern"))
    assert prop_re == icd_re


# ── numeric_with_unit ───────────────────────────────────────────────


def test_numeric_valid() -> None:
    meta = parse_field_metadata(
        "numeric_with_unit",
        {"value": 140.0, "unit": "мм рт. ст.", "confidence": 0.95, "source": "extracted"},
    )
    assert isinstance(meta, NumericMeta)
    assert meta.value == 140.0


def test_numeric_missing_unit_rejected() -> None:
    with pytest.raises(FieldMetadataError):
        parse_field_metadata(
            "numeric_with_unit", {"value": 140.0, "confidence": 0.95, "source": "extracted"}
        )


def test_numeric_non_number_rejected() -> None:
    with pytest.raises(FieldMetadataError):
        parse_field_metadata(
            "numeric_with_unit",
            {"value": "сто сорок", "unit": "мм", "confidence": 0.9, "source": "extracted"},
        )


# ── date / date_with_note ───────────────────────────────────────────


@pytest.mark.parametrize("ft", ["date", "date_with_note"])
def test_date_valid(ft: str) -> None:
    meta = parse_field_metadata(ft, {"date": "2026-07-01", "source": "manual"})
    assert isinstance(meta, DateMeta)


def test_date_not_a_real_date_rejected() -> None:
    with pytest.raises(FieldMetadataError, match="real calendar date"):
        parse_field_metadata(
            "date", {"date": "2026-02-30", "confidence": 0.9, "source": "extracted"}
        )


def test_date_wrong_format_rejected() -> None:
    with pytest.raises(FieldMetadataError):
        parse_field_metadata("date", {"date": "01.07.2026", "source": "manual"})


# ── common rules ────────────────────────────────────────────────────


def test_empty_dict_always_valid_for_every_field_type() -> None:
    for ft in [*META_MODEL_BY_FIELD_TYPE, "free_text", "anything_else"]:
        assert parse_field_metadata(ft, {}) is None
        validate_field_metadata(ft, {})  # must not raise


def test_free_text_rejects_any_metadata() -> None:
    with pytest.raises(FieldMetadataError, match="accepts no metadata"):
        validate_field_metadata("free_text", {"selected": "x", "source": "manual"})


def test_registry_covers_exactly_the_typed_field_types() -> None:
    assert set(META_MODEL_BY_FIELD_TYPE) == {
        "choice",
        "multi_choice",
        "structured_diagnosis",
        "numeric_with_unit",
        "date",
        "date_with_note",
    }


# ── property: validate accepts exactly what parse parses ────────────

_META_DICTS = st.one_of(
    st.fixed_dictionaries(
        {"selected": st.text(min_size=0, max_size=80)},
        optional={
            "confidence": st.one_of(st.floats(allow_nan=False, allow_infinity=False), st.none()),
            "source": st.sampled_from(["extracted", "manual", "guessed", ""]),
            "junk": st.text(max_size=5),
        },
    ),
    st.dictionaries(st.text(max_size=10), st.text(max_size=10), max_size=3),
)


@given(ft=st.sampled_from([*META_MODEL_BY_FIELD_TYPE, "free_text"]), md=_META_DICTS)
def test_validate_and_parse_never_drift(ft: str, md: dict[str, object]) -> None:
    """validate_field_metadata raises iff parse_field_metadata raises."""
    try:
        parse_field_metadata(ft, md)
        parsed_ok = True
    except FieldMetadataError:
        parsed_ok = False
    try:
        validate_field_metadata(ft, md)
        validated_ok = True
    except FieldMetadataError:
        validated_ok = False
    assert parsed_ok == validated_ok


def test_error_message_names_field_type_and_reason() -> None:
    with pytest.raises(FieldMetadataError) as exc:
        parse_field_metadata("choice", {"selected": "x"})
    assert exc.value.field_type == "choice"
    assert re.search(r"source", exc.value.reason)
