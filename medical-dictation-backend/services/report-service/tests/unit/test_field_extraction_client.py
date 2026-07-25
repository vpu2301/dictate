"""Draft-assembly consumer of the sprint-13 extractor (ADR-0032).

Contract: proposals reach ``field_specific_metadata``; failures never
cost the draft.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
import pytest

from report_service.domain import field_extraction_client as fec
from report_service.routers.reports_from_transcript import _content_for_template
from template_models import ChoiceOption, FieldType, TemplateDefinition, TemplateSection


def _definition() -> TemplateDefinition:
    return TemplateDefinition(
        code="anamnesis_intake",
        name="Анамнез",
        language="uk",
        specialty="family_medicine",
        sections=(
            TemplateSection(
                id="complaints",
                name="Скарги",
                voice_aliases=("скарги",),
                asr_prompt="скарги",
                order=0,
            ),
            TemplateSection(
                id="smoking_status",
                name="Куріння",
                voice_aliases=("куріння",),
                field_type=FieldType.CHOICE,
                asr_prompt="куріння",
                order=1,
                options=(
                    ChoiceOption(value="never", label="Не палить", voice_aliases=("не палить",)),
                    ChoiceOption(value="current", label="Палить", voice_aliases=("палить",)),
                ),
            ),
        ),
    )


# ── payload shaping ─────────────────────────────────────────────────


def test_only_typed_sections_are_sent() -> None:
    payload = fec.typed_sections_payload(_definition())
    assert [s["section_key"] for s in payload] == ["smoking_status"]
    assert payload[0]["field_type"] == "choice"
    assert payload[0]["options"] == [
        {"value": "never", "label": "Не палить", "aliases": ["не палить"]},
        {"value": "current", "label": "Палить", "aliases": ["палить"]},
    ]


def test_template_without_typed_sections_sends_nothing() -> None:
    definition = TemplateDefinition(
        code="plain",
        name="Plain",
        language="uk",
        specialty="family_medicine",
        sections=(TemplateSection(id="a", name="A", voice_aliases=("а",), asr_prompt="p"),),
    )
    assert fec.typed_sections_payload(definition) == []


# ── HTTP behaviour (fail-open) ──────────────────────────────────────


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    class _Client:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def post(self, path: str, **kwargs: Any) -> Any:
            return handler(path, **kwargs)

    monkeypatch.setattr(fec.httpx, "AsyncClient", _Client)


class _Resp:
    def __init__(self, status: int, payload: Any = None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


@pytest.mark.asyncio
async def test_happy_path_returns_the_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(
        monkeypatch,
        lambda path, **kw: _Resp(
            200,
            {
                "metadata": {
                    "field_extraction.fields": {
                        "smoking_status": {
                            "selected": "current",
                            "confidence": 1.0,
                            "source": "extracted",
                        }
                    }
                }
            },
        ),
    )
    fields = await fec.extract_fields(
        definition=_definition(),
        text="пацієнт курить",
        language="uk",
        specialty="family_medicine",
        authorization="Bearer x",
    )
    assert fields["smoking_status"]["selected"] == "current"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler",
    [
        lambda path, **kw: _Resp(500, text="boom"),
        lambda path, **kw: _Resp(200, None),  # unparseable body
        lambda path, **kw: _Resp(200, {"metadata": {}}),  # no extraction key
        lambda path, **kw: _Resp(200, {"metadata": {"field_extraction.fields": "nope"}}),
    ],
)
async def test_failures_yield_no_metadata_not_an_error(
    monkeypatch: pytest.MonkeyPatch, handler: Any
) -> None:
    _patch_client(monkeypatch, handler)
    assert (
        await fec.extract_fields(
            definition=_definition(),
            text="пацієнт курить",
            language="uk",
            specialty=None,
            authorization="Bearer x",
        )
        == {}
    )


@pytest.mark.asyncio
async def test_transport_error_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(path: str, **kw: Any) -> Any:
        raise httpx.ConnectError("refused")

    _patch_client(monkeypatch, _boom)
    assert (
        await fec.extract_fields(
            definition=_definition(),
            text="пацієнт курить",
            language="uk",
            specialty=None,
            authorization="Bearer x",
        )
        == {}
    )


@pytest.mark.asyncio
async def test_empty_text_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    def _never(path: str, **kw: Any) -> Any:
        raise AssertionError("must not call nlp-service for empty text")

    _patch_client(monkeypatch, _never)
    assert (
        await fec.extract_fields(
            definition=_definition(),
            text="   ",
            language="uk",
            specialty=None,
            authorization="Bearer x",
        )
        == {}
    )


# ── content assembly ────────────────────────────────────────────────


_TEMPLATE_ID = UUID("70cd91de-82b0-48e5-81ce-dcc01e0a2297")


def test_proposals_land_in_field_specific_metadata() -> None:
    content = _content_for_template(
        definition=_definition(),
        template_id=_TEMPLATE_ID,
        schema_version=1,
        transcript="пацієнт курить, скарги на кашель",
        title="T",
        encounter_date=None,
        extracted_fields={
            "smoking_status": {"selected": "current", "confidence": 0.95, "source": "extracted"}
        },
    )
    by_key = {s.section_key: s for s in content.sections}
    assert by_key["smoking_status"].field_specific_metadata == {
        "selected": "current",
        "confidence": 0.95,
        "source": "extracted",
    }
    # The prose is untouched and still lives in the free-text section.
    assert by_key["complaints"].text == "пацієнт курить, скарги на кашель"
    assert by_key["complaints"].field_specific_metadata == {}


def test_no_proposals_leaves_metadata_empty() -> None:
    content = _content_for_template(
        definition=_definition(),
        template_id=_TEMPLATE_ID,
        schema_version=1,
        transcript="скарги на кашель",
        title="T",
        encounter_date=None,
        extracted_fields={},
    )
    assert all(s.field_specific_metadata == {} for s in content.sections)


def test_assembled_content_passes_the_step_02_write_validator() -> None:
    from report_service.domain.content_metadata import validate_content_metadata

    content = _content_for_template(
        definition=_definition(),
        template_id=_TEMPLATE_ID,
        schema_version=1,
        transcript="пацієнт курить",
        title="T",
        encounter_date=None,
        extracted_fields={
            "smoking_status": {"selected": "current", "confidence": 0.95, "source": "extracted"}
        },
    )
    assert validate_content_metadata(content, _definition()) == []
