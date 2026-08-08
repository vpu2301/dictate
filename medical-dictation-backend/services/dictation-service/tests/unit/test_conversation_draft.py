"""Sprint-14 conversation finalize → report draft (pure, fakes only).

Two contracts:

* ``dialogue_text`` renders reviewable speaker turns — labelled per
  language, consecutive same-speaker turns merged, and an unmapped
  speaker gets the honesty label instead of being folded into a party.
* ``create_conversation_draft`` posts through the EXISTING
  ``POST /v1/reports`` surface with the clinician's bearer, links back
  via ``source_session_id`` + ``transcript_segment_ids``, and NEVER
  raises: every failure degrades to a ``conversation.draft.create_failed``
  audit row (the transcript is already persisted when this runs).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from dictation_service import audit_kinds
from dictation_service.integrations.report_client import DraftResult
from dictation_service.session.draft import create_conversation_draft, dialogue_text
from dictation_service.session.finalize import FinalizeResult
from dictation_service.session.manager import SessionContext

SEG_ID_1 = str(uuid4())
SEG_ID_2 = str(uuid4())


# ── Fakes ────────────────────────────────────────────────────────────


class _FakeAuditWriter:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def write_event(self, **kwargs: Any) -> None:
        self.events.append(kwargs)

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.events]

    def only(self) -> dict[str, Any]:
        assert len(self.events) == 1, f"expected one audit event, got {self.kinds()}"
        return self.events[0]


class _FakeReportClient:
    def __init__(self, result: DraftResult | None) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def create_draft(self, *, bearer: str, body: dict[str, Any]) -> DraftResult | None:
        self.calls.append({"bearer": bearer, "body": body})
        return self._result


@dataclass
class _FakeTemplateDoc:
    sections: list[dict[str, Any]]
    schema_version: int = 3


def _transcript() -> list[dict[str, Any]]:
    return [
        {"id": SEG_ID_1, "text": "Розкажіть, що турбує.", "speaker_role": "doctor"},
        {"id": SEG_ID_2, "text": "Болить голова.", "speaker_role": "patient"},
    ]


def _state(*, draft: DraftResult | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        report_client=_FakeReportClient(draft),
        audit_writer=_FakeAuditWriter(),
    )


def _ctx(
    *,
    patient_id: Any = "set",
    bearer: str | None = "tok",
    template_id: Any = "set",
    template_doc: Any = "set",
    language: str = "uk",
) -> SessionContext:
    ctx = SessionContext(
        session_id=uuid4(),
        tenant_id=uuid4(),
        user_id=uuid4(),
        language=language,
        prompt_id=uuid4(),
        prompt_text="",
        target_kind="generic",
        encounter_id=uuid4(),
        template_id=uuid4() if template_id == "set" else template_id,
        mode="conversation",
        bearer=bearer,
        patient_id=uuid4() if patient_id == "set" else patient_id,
    )
    ctx.template_doc = (
        _FakeTemplateDoc(sections=[{"id": "anamnesis", "asr_prompt": ""}])
        if template_doc == "set"
        else template_doc
    )
    return ctx


def _result(transcript: list[dict[str, Any]] | None) -> FinalizeResult:
    return FinalizeResult(
        audio_file_id=None,
        truncated=False,
        transcript_segments=len(transcript or []),
        duration_ms=12_000,
        transcript=transcript,
    )


# ── dialogue_text ────────────────────────────────────────────────────


def test_uk_dialogue_uses_clinical_role_labels() -> None:
    text = dialogue_text(_transcript(), "uk")
    assert text == "ЛІКАР: Розкажіть, що турбує.\nПАЦІЄНТ: Болить голова."


def test_en_dialogue_uses_english_labels() -> None:
    text = dialogue_text(
        [
            {"id": SEG_ID_1, "text": "What brings you in?", "speaker_role": "doctor"},
            {"id": SEG_ID_2, "text": "My back hurts.", "speaker_role": "patient"},
        ],
        "en",
    )
    assert text == "DOCTOR: What brings you in?\nPATIENT: My back hurts."


def test_consecutive_same_speaker_segments_merge_onto_one_line() -> None:
    text = dialogue_text(
        [
            {"text": "Добрий день.", "speaker_role": "doctor"},
            {"text": "Сідайте, будь ласка.", "speaker_role": "doctor"},
            {"text": "Дякую.", "speaker_role": "patient"},
        ],
        "uk",
    )
    assert text == "ЛІКАР: Добрий день. Сідайте, будь ласка.\nПАЦІЄНТ: Дякую."


def test_unmapped_speaker_renders_the_honesty_label() -> None:
    text = dialogue_text(
        [
            {"text": "Добрий день.", "speaker_role": "doctor"},
            {"text": "Щось нерозбірливе.", "speaker_role": None, "speaker": "UNKNOWN"},
        ],
        "uk",
    )
    assert text == "ЛІКАР: Добрий день.\nНЕВІДОМО: Щось нерозбірливе."


def test_empty_segments_are_skipped() -> None:
    text = dialogue_text(
        [
            {"text": "   ", "speaker_role": "doctor"},
            {"text": "Болить.", "speaker_role": "patient"},
        ],
        "uk",
    )
    assert text == "ПАЦІЄНТ: Болить."


def test_unknown_language_falls_back_to_english_labels() -> None:
    # "fr" is not a dictation language; "de" is (see LANGUAGE_PATTERN).
    assert dialogue_text([{"text": "x", "speaker_role": "doctor"}], "fr") == "DOCTOR: x"


def test_german_labels() -> None:
    text = dialogue_text(
        [
            {"text": "Was führt Sie zu mir?", "speaker_role": "doctor"},
            {"text": "Ich habe Kopfschmerzen.", "speaker_role": "patient"},
            {"text": "Hm.", "speaker_role": None, "speaker": "UNKNOWN"},
        ],
        "de",
    )
    assert text == ("ARZT: Was führt Sie zu mir?\nPATIENT: Ich habe Kopfschmerzen.\nUNBEKANNT: Hm.")


# ── create_conversation_draft — happy path ───────────────────────────


async def test_draft_posts_the_sprint08_report_body() -> None:
    state = _state(draft=DraftResult(report_id="r1", code="C-1", version_id="v1"))
    ctx = _ctx()

    await create_conversation_draft(ctx, state, finalize_result=_result(_transcript()))

    assert len(state.report_client.calls) == 1
    call = state.report_client.calls[0]
    assert call["bearer"] == "tok"

    body = call["body"]
    assert body["patient_id"] == str(ctx.patient_id)
    assert body["source_session_id"] == str(ctx.session_id)
    assert body["co_author_ids"] == []

    content = body["content"]
    assert content["template_id"] == str(ctx.template_id)
    assert content["template_schema_version"] == 3
    assert content["title"] == "Консультація (розмовний режим)"
    assert content["icd10_codes"] == []

    section = content["sections"][0]
    assert section["section_key"] == "anamnesis"
    assert section["transcript_segment_ids"] == [SEG_ID_1, SEG_ID_2]
    assert section["text"] == "ЛІКАР: Розкажіть, що турбує.\nПАЦІЄНТ: Болить голова."

    event = state.audit_writer.only()
    assert event["kind"] == audit_kinds.DRAFT_CREATED == "conversation.draft.created"
    assert event["target_kind"] == "report"
    assert event["target_id"] == "r1"
    assert event["payload"]["code"] == "C-1"
    assert event["payload"]["version_id"] == "v1"
    assert event["payload"]["segments"] == 2
    assert event["payload"]["session_id"] == str(ctx.session_id)


async def test_english_draft_gets_the_english_title() -> None:
    state = _state(draft=DraftResult(report_id="r2", code="C-2", version_id="v2"))
    ctx = _ctx(language="en")

    await create_conversation_draft(ctx, state, finalize_result=_result(_transcript()))

    body = state.report_client.calls[0]["body"]
    assert body["content"]["title"] == "Consultation (conversation)"


# ── failure paths — audited, never raised ────────────────────────────


@pytest.mark.parametrize(
    ("ctx_kwargs", "transcript", "reason"),
    [
        ({}, None, "empty_transcript"),
        ({}, [], "empty_transcript"),
        ({"patient_id": None}, "full", "no_patient_id"),
        ({"bearer": None}, "full", "no_bearer"),
        ({"template_id": None}, "full", "no_template"),
        ({"template_doc": None}, "full", "no_template"),
    ],
)
async def test_failures_audit_and_never_raise(
    ctx_kwargs: dict[str, Any],
    transcript: Any,
    reason: str,
) -> None:
    state = _state(draft=DraftResult(report_id="r1", code="C-1", version_id="v1"))
    ctx = _ctx(**ctx_kwargs)
    segments = _transcript() if transcript == "full" else transcript

    await create_conversation_draft(ctx, state, finalize_result=_result(segments))

    event = state.audit_writer.only()
    assert event["kind"] == audit_kinds.DRAFT_CREATE_FAILED
    assert event["payload"]["reason"] == reason
    assert event["severity"].value == "warn"
    assert event["target_id"] == str(ctx.session_id)
    assert state.report_client.calls == []  # never posted


async def test_template_without_a_section_key_is_audited() -> None:
    state = _state(draft=DraftResult(report_id="r1", code="C-1", version_id="v1"))
    ctx = _ctx()
    ctx.template_doc = _FakeTemplateDoc(sections=[{"name": "Анамнез"}])

    await create_conversation_draft(ctx, state, finalize_result=_result(_transcript()))

    assert state.audit_writer.only()["payload"]["reason"] == "template_has_no_section_key"
    assert state.report_client.calls == []


async def test_report_service_error_is_audited_after_the_post() -> None:
    state = _state(draft=None)
    ctx = _ctx()

    await create_conversation_draft(ctx, state, finalize_result=_result(_transcript()))

    assert len(state.report_client.calls) == 1  # it DID try
    event = state.audit_writer.only()
    assert event["kind"] == audit_kinds.DRAFT_CREATE_FAILED
    assert event["payload"]["reason"] == "report_service_error"
    assert audit_kinds.DRAFT_CREATED not in state.audit_writer.kinds()


async def test_german_draft_gets_the_german_title() -> None:
    state = _state(draft=DraftResult(report_id="r3", code="C-3", version_id="v3"))
    ctx = _ctx(language="de")

    await create_conversation_draft(ctx, state, finalize_result=_result(_transcript()))

    body = state.report_client.calls[0]["body"]
    assert body["content"]["title"] == "Konsultation (Gesprächsmodus)"
