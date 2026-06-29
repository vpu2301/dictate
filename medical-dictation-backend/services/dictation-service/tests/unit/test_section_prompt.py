"""Section-aware dictation lookup (sprint 06).

``section_prompt`` is the validation + prompt-resolution core the WS
``switch_section`` handler relies on: a valid section_id yields its ASR
prompt (which the handler swaps into ``StreamingWindower.base_prompt``);
an unknown id yields None (the handler rejects with a recoverable error).
"""

from __future__ import annotations

from uuid import uuid4

from dictation_service.integrations.template_client import TemplateDoc, section_prompt


def _doc() -> TemplateDoc:
    return TemplateDoc(
        template_id=uuid4(),
        code="family_medicine_soap_uk",
        name="Сімейний лікар",
        language="uk",
        specialty="family_medicine",
        schema_version=1,
        sections=[
            {"id": "subjective", "name": "Скарги", "asr_prompt": "скарги та анамнез"},
            {"id": "assessment", "name": "Діагноз", "asr_prompt": "клінічний діагноз"},
        ],
    )


def test_known_section_returns_prompt_and_name() -> None:
    resolved = section_prompt(_doc(), "assessment")
    assert resolved == ("клінічний діагноз", "Діагноз")


def test_unknown_section_returns_none() -> None:
    assert section_prompt(_doc(), "does_not_exist") is None


def test_section_missing_prompt_defaults_empty() -> None:
    doc = TemplateDoc(
        template_id=uuid4(),
        code="c",
        name="C",
        language="uk",
        specialty="x",
        schema_version=1,
        sections=[{"id": "s1", "name": "S1"}],
    )
    assert section_prompt(doc, "s1") == ("", "S1")
