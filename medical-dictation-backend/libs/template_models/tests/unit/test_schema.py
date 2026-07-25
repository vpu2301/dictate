"""Template schema + edit classification tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from template_models import (
    FIELD_TYPES,
    ChoiceOption,
    EditKind,
    FieldType,
    TemplateDefinition,
    TemplateMetadata,
    TemplateSection,
    classify_edit,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SEED_DIR = _REPO_ROOT / "infra" / "seeds" / "templates"
_FROZEN_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "pre_s13_dumps"


def _section(
    id: str = "anamnesis",
    *,
    required: bool = True,
    field_type: FieldType = FieldType.FREE_TEXT,
    aliases: tuple[str, ...] = ("анамнез",),
    min_chars: int = 0,
    prompt: str = "коротка історія хвороби",
    synthesis_prompt: str = "",
    options: tuple[ChoiceOption, ...] = (),
) -> TemplateSection:
    return TemplateSection(
        id=id,
        name=id.capitalize(),
        voice_aliases=aliases,
        required=required,
        field_type=field_type,
        asr_prompt=prompt,
        min_chars=min_chars,
        synthesis_prompt=synthesis_prompt,
        options=options,
    )


def _options(n: int = 2) -> tuple[ChoiceOption, ...]:
    return tuple(ChoiceOption(value=f"opt_{i}", label=f"Option {i}") for i in range(n))


def _template(sections: tuple[TemplateSection, ...] | None = None) -> TemplateDefinition:
    return TemplateDefinition(
        code="cardiology_outpatient",
        name="Cardiology outpatient",
        language="uk",
        specialty="cardiology",
        sections=sections or (_section(),),
    )


# ── Validation tests ────────────────────────────────────────────────


def test_minimal_valid_template() -> None:
    t = _template()
    assert t.code == "cardiology_outpatient"
    assert len(t.sections) == 1


def test_extra_field_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        TemplateDefinition.model_validate(
            {
                "code": "c",
                "name": "C",
                "language": "uk",
                "specialty": "cardiology",
                "sections": [
                    {
                        "id": "a",
                        "name": "A",
                        "asr_prompt": "p",
                        "is_admin": True,  # injection attempt
                    }
                ],
            }
        )
    assert "Extra inputs are not permitted" in str(exc.value) or "extra" in str(exc.value).lower()


def test_section_id_must_be_slug() -> None:
    with pytest.raises(ValidationError):
        _section(id="Has Space")


def test_section_id_must_start_with_letter() -> None:
    with pytest.raises(ValidationError):
        _section(id="1_section")


def test_duplicate_voice_alias_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        _template(
            sections=(
                _section(id="anamnesis", aliases=("анамнез",)),
                _section(id="exam", aliases=("анамнез",)),  # collision
            )
        )
    assert "duplicated" in str(exc.value)


def test_duplicate_section_id_rejected() -> None:
    with pytest.raises(ValidationError):
        _template(
            sections=(
                _section(id="anamnesis"),
                _section(id="anamnesis"),
            )
        )


def test_voice_aliases_lowercased() -> None:
    s = _section(aliases=("Анамнез", "ANAMNESIS"))
    assert s.voice_aliases == ("анамнез", "anamnesis")


def test_voice_aliases_dedupe_preserves_order() -> None:
    s = _section(aliases=("анамнез", "anamnesis", "анамнез"))
    assert s.voice_aliases == ("анамнез", "anamnesis")


def test_invalid_language() -> None:
    with pytest.raises(ValidationError):
        TemplateDefinition(
            code="c",
            name="C",
            language="fr",  # not supported
            specialty="cardiology",
            sections=(_section(),),
        )


def test_asr_prompt_max_length_enforced() -> None:
    with pytest.raises(ValidationError):
        _section(prompt="а" * 2000)


def test_metadata_defaults_empty() -> None:
    t = _template()
    assert t.metadata == TemplateMetadata()


def test_synthesis_prompt_defaults_empty() -> None:
    """synthesis_prompt is optional; sprint-12 treats empty as no guidance."""
    s = _section()
    assert s.synthesis_prompt == ""


def test_synthesis_prompt_roundtrips() -> None:
    s = _section(synthesis_prompt="Опиши анамнез у форматі SOAP, третя особа.")
    assert s.synthesis_prompt == "Опиши анамнез у форматі SOAP, третя особа."


def test_synthesis_prompt_max_length_enforced() -> None:
    with pytest.raises(ValidationError):
        _section(synthesis_prompt="а" * 2001)


# ── Edit classification tests ───────────────────────────────────────


def test_no_change_classification() -> None:
    a = _template()
    b = _template()
    assert classify_edit(a, b).kind == EditKind.NO_CHANGE


def test_cosmetic_name_change() -> None:
    a = _template()
    b = _template(sections=(_section(prompt="new prompt text"),))
    result = classify_edit(a, b)
    assert result.kind == EditKind.COSMETIC


def test_structural_section_added() -> None:
    a = _template()
    b = _template(
        sections=(
            _section(id="anamnesis"),
            _section(id="exam", aliases=("огляд",), prompt="exam prompt"),
        )
    )
    result = classify_edit(a, b)
    assert result.kind == EditKind.STRUCTURAL
    assert any("added" in r for r in result.reasons)


def test_structural_section_removed() -> None:
    a = _template(
        sections=(
            _section(id="anamnesis"),
            _section(id="exam", aliases=("огляд",), prompt="exam prompt"),
        )
    )
    b = _template()
    result = classify_edit(a, b)
    assert result.kind == EditKind.STRUCTURAL
    assert any("removed" in r for r in result.reasons)


def test_structural_field_type_changed() -> None:
    a = _template()
    b = _template(sections=(_section(field_type=FieldType.STRUCTURED_DIAGNOSIS),))
    result = classify_edit(a, b)
    assert result.kind == EditKind.STRUCTURAL
    assert any("field_type" in r for r in result.reasons)


def test_structural_required_flipped() -> None:
    a = _template()
    b = _template(sections=(_section(required=False),))
    result = classify_edit(a, b)
    assert result.kind == EditKind.STRUCTURAL


def test_structural_min_chars_increased() -> None:
    a = _template(sections=(_section(min_chars=10),))
    b = _template(sections=(_section(min_chars=50),))
    assert classify_edit(a, b).kind == EditKind.STRUCTURAL


def test_cosmetic_min_chars_decreased() -> None:
    """Loosening the constraint is cosmetic — old reports stay valid."""
    a = _template(sections=(_section(min_chars=50),))
    b = _template(sections=(_section(min_chars=10),))
    assert classify_edit(a, b).kind == EditKind.COSMETIC


def test_cosmetic_synthesis_prompt_changed() -> None:
    """Synthesis prompt drives sprint-12 prose, not report shape → cosmetic."""
    a = _template(sections=(_section(synthesis_prompt="old guidance"),))
    b = _template(sections=(_section(synthesis_prompt="new guidance"),))
    assert classify_edit(a, b).kind == EditKind.COSMETIC


# ── Sprint-13: choice / multi_choice ────────────────────────────────


def test_field_types_has_seven_members() -> None:
    assert {
        "free_text",
        "structured_diagnosis",
        "date",
        "date_with_note",
        "numeric_with_unit",
        "choice",
        "multi_choice",
    } == FIELD_TYPES


@pytest.mark.parametrize("ft", [FieldType.CHOICE, FieldType.MULTI_CHOICE])
def test_choice_section_valid(ft: FieldType) -> None:
    s = _section(field_type=ft, options=_options())
    assert len(s.options) == 2


@pytest.mark.parametrize("ft", [FieldType.CHOICE, FieldType.MULTI_CHOICE])
def test_choice_without_options_rejected(ft: FieldType) -> None:
    with pytest.raises(ValidationError, match="requires 2..50 options"):
        _section(field_type=ft)


def test_free_text_with_options_rejected() -> None:
    with pytest.raises(ValidationError, match="must not define options"):
        _section(field_type=FieldType.FREE_TEXT, options=_options())


@pytest.mark.parametrize(
    "ft",
    [
        FieldType.STRUCTURED_DIAGNOSIS,
        FieldType.DATE,
        FieldType.DATE_WITH_NOTE,
        FieldType.NUMERIC_WITH_UNIT,
    ],
)
def test_other_field_types_with_options_rejected(ft: FieldType) -> None:
    with pytest.raises(ValidationError, match="must not define options"):
        _section(field_type=ft, options=_options())


def test_single_option_rejected() -> None:
    with pytest.raises(ValidationError, match="requires 2..50 options"):
        _section(field_type=FieldType.CHOICE, options=_options(1))


def test_fifty_one_options_rejected() -> None:
    with pytest.raises(ValidationError, match="requires 2..50 options"):
        _section(field_type=FieldType.CHOICE, options=_options(51))


def test_fifty_options_accepted() -> None:
    s = _section(field_type=FieldType.CHOICE, options=_options(50))
    assert len(s.options) == 50


def test_duplicate_option_value_rejected() -> None:
    opts = (
        ChoiceOption(value="never", label="Не палить"),
        ChoiceOption(value="never", label="Палить"),
    )
    with pytest.raises(ValidationError, match="option value 'never' duplicated"):
        _section(field_type=FieldType.CHOICE, options=opts)


def test_duplicate_option_label_case_insensitive_rejected() -> None:
    opts = (
        ChoiceOption(value="a", label="Палить"),
        ChoiceOption(value="b", label="ПАЛИТЬ"),
    )
    with pytest.raises(ValidationError, match="label .* duplicated"):
        _section(field_type=FieldType.CHOICE, options=opts)


def test_duplicate_alias_across_options_rejected() -> None:
    opts = (
        ChoiceOption(value="a", label="A", voice_aliases=("палить",)),
        ChoiceOption(value="b", label="B", voice_aliases=("Палить",)),
    )
    with pytest.raises(ValidationError, match="duplicated across options"):
        _section(field_type=FieldType.CHOICE, options=opts)


def test_option_value_must_be_slug() -> None:
    with pytest.raises(ValidationError, match="URL-safe slug"):
        ChoiceOption(value="Not A Slug", label="X")


def test_option_aliases_normalized_nfc_lower_stripped() -> None:
    # "\u0438" + U+0306 combining breve (decomposed "\u0439") must normalize
    # to the composed form, so the extractor never re-normalizes.
    decomposed = "\u0438\u0306\u043e\u0434"
    opt = ChoiceOption(
        value="x",
        label="X",
        voice_aliases=("  \u041d\u0435 \u041f\u0430\u043b\u0438\u0442\u044c  ", decomposed),
    )
    assert opt.voice_aliases[0] == "\u043d\u0435 \u043f\u0430\u043b\u0438\u0442\u044c"
    assert opt.voice_aliases[1] == "\u0439\u043e\u0434"


def test_option_alias_over_64_chars_rejected() -> None:
    with pytest.raises(ValidationError, match="exceeds 64 characters"):
        ChoiceOption(value="x", label="X", voice_aliases=("а" * 65,))


def test_option_empty_alias_rejected() -> None:
    with pytest.raises(ValidationError, match="empty strings"):
        ChoiceOption(value="x", label="X", voice_aliases=("   ",))


def test_option_aliases_dedupe_preserves_order() -> None:
    opt = ChoiceOption(value="x", label="X", voice_aliases=("палить", "курить", "Палить"))
    assert opt.voice_aliases == ("палить", "курить")


# ── Sprint-13: dump shape (the additive contract) ───────────────────


def test_options_omitted_from_dump_when_empty() -> None:
    s = _section()
    assert "options" not in s.model_dump()
    assert "options" not in json.loads(s.model_dump_json())


def test_options_present_in_dump_when_set() -> None:
    s = _section(field_type=FieldType.CHOICE, options=_options())
    dump = s.model_dump(mode="json")
    assert dump["options"] == [
        {"value": "opt_0", "label": "Option 0", "voice_aliases": []},
        {"value": "opt_1", "label": "Option 1", "voice_aliases": []},
    ]


def test_options_roundtrip_through_dump() -> None:
    s = _section(field_type=FieldType.MULTI_CHOICE, options=_options(3))
    restored = TemplateSection.model_validate(s.model_dump(mode="json"))
    assert restored == s


# ── Sprint-13: the additive proof over all shipped templates ────────


def test_all_seed_templates_validate_and_dump_byte_identical() -> None:
    """Every as-shipped system template must validate under the S13
    model and serialize byte-identically to its pre-bump dump (frozen
    fixture committed with the S13 PR). This is the ADR-0016 additive
    guarantee, proven rather than asserted."""
    seed_files = sorted(_SEED_DIR.glob("*.json"))
    frozen_files = sorted(_FROZEN_DIR.glob("*.json"))
    assert seed_files, f"no seed templates found at {_SEED_DIR}"
    frozen_names = {p.name for p in frozen_files}
    pre_s13 = [p for p in seed_files if p.name in frozen_names]
    assert len(pre_s13) == 20, "the 20 pre-S13 templates must all have frozen dumps"

    for path in pre_s13:
        tpl = TemplateDefinition.model_validate(json.loads(path.read_text("utf-8")))
        canonical = (
            json.dumps(tpl.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2)
            + "\n"
        )
        frozen = (_FROZEN_DIR / path.name).read_text("utf-8")
        assert canonical == frozen, f"{path.name}: dump drifted from pre-S13 fixture"
        for section in tpl.model_dump(mode="json")["sections"]:
            assert "options" not in section, f"{path.name}: old template gained an options key"


def test_new_seed_templates_validate() -> None:
    """Templates added in/after S13 (no frozen dump) must still validate."""
    frozen_names = {p.name for p in _FROZEN_DIR.glob("*.json")}
    for path in sorted(_SEED_DIR.glob("*.json")):
        if path.name in frozen_names:
            continue
        TemplateDefinition.model_validate(json.loads(path.read_text("utf-8")))


# ── Sprint-13: classify_edit options matrix ─────────────────────────


def _choice_template(options: tuple[ChoiceOption, ...]) -> TemplateDefinition:
    return _template(sections=(_section(field_type=FieldType.CHOICE, options=options),))


_NEVER = ChoiceOption(value="never", label="Не палить", voice_aliases=("не палить",))
_CURRENT = ChoiceOption(value="current", label="Палить", voice_aliases=("палить",))
_FORMER = ChoiceOption(value="former", label="У минулому", voice_aliases=("кинув",))


def test_option_added_is_cosmetic() -> None:
    a = _choice_template((_NEVER, _CURRENT))
    b = _choice_template((_NEVER, _CURRENT, _FORMER))
    assert classify_edit(a, b).kind == EditKind.COSMETIC


def test_option_removed_is_structural() -> None:
    a = _choice_template((_NEVER, _CURRENT, _FORMER))
    b = _choice_template((_NEVER, _CURRENT))
    result = classify_edit(a, b)
    assert result.kind == EditKind.STRUCTURAL
    assert any("former" in r for r in result.reasons)


def test_option_value_renamed_is_structural() -> None:
    a = _choice_template((_NEVER, _CURRENT))
    renamed = ChoiceOption(value="current_smoker", label="Палить", voice_aliases=("палить",))
    b = _choice_template((_NEVER, renamed))
    result = classify_edit(a, b)
    assert result.kind == EditKind.STRUCTURAL
    assert any("removed/renamed" in r for r in result.reasons)


def test_option_label_change_is_cosmetic() -> None:
    a = _choice_template((_NEVER, _CURRENT))
    relabeled = ChoiceOption(value="current", label="Курить", voice_aliases=("палить",))
    b = _choice_template((_NEVER, relabeled))
    assert classify_edit(a, b).kind == EditKind.COSMETIC


def test_option_alias_added_is_cosmetic() -> None:
    a = _choice_template((_NEVER, _CURRENT))
    aliased = ChoiceOption(value="current", label="Палить", voice_aliases=("палить", "курить"))
    b = _choice_template((_NEVER, aliased))
    assert classify_edit(a, b).kind == EditKind.COSMETIC


@pytest.mark.parametrize("new_ft", [FieldType.CHOICE, FieldType.MULTI_CHOICE])
def test_field_type_change_to_choice_is_structural(new_ft: FieldType) -> None:
    a = _template()
    b = _template(sections=(_section(field_type=new_ft, options=_options()),))
    result = classify_edit(a, b)
    assert result.kind == EditKind.STRUCTURAL
    assert any("field_type" in r for r in result.reasons)


def test_field_type_change_from_choice_is_structural() -> None:
    a = _template(sections=(_section(field_type=FieldType.CHOICE, options=_options()),))
    b = _template()
    result = classify_edit(a, b)
    assert result.kind == EditKind.STRUCTURAL
    assert any("field_type" in r for r in result.reasons)


def test_choice_to_multi_choice_is_structural() -> None:
    a = _template(sections=(_section(field_type=FieldType.CHOICE, options=_options()),))
    b = _template(sections=(_section(field_type=FieldType.MULTI_CHOICE, options=_options()),))
    result = classify_edit(a, b)
    assert result.kind == EditKind.STRUCTURAL
