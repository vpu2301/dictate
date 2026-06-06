"""Template schema + edit classification tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from template_models import (
    EditKind,
    FieldType,
    TemplateDefinition,
    TemplateMetadata,
    TemplateSection,
    classify_edit,
)


def _section(
    id: str = "anamnesis",
    *,
    required: bool = True,
    field_type: FieldType = FieldType.FREE_TEXT,
    aliases: tuple[str, ...] = ("анамнез",),
    min_chars: int = 0,
    prompt: str = "коротка історія хвороби",
) -> TemplateSection:
    return TemplateSection(
        id=id,
        name=id.capitalize(),
        voice_aliases=aliases,
        required=required,
        field_type=field_type,
        asr_prompt=prompt,
        min_chars=min_chars,
    )


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
