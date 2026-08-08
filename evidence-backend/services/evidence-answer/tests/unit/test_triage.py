"""Triage: rule/classifier ordering, safe messaging, and fail-open behaviour."""

from __future__ import annotations

import pytest
from conftest import FakeGateway
from evidence_answer.domain.stages import triage_rules
from evidence_answer.domain.stages.triage import message_for, rules_only, triage

from evidence_models import TriageOutcome, TriageReason

PROMPT = "classify: {question}"


# ── the deterministic half ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "question",
    [
        "Patient is coding right now, what do I do?",
        "Пацієнт непритомний, немає пульсу, що робити зараз?",
        "Anaphylaxis happening now in clinic, what do I give",
        "Хворий не дихає, терміново потрібна допомога",
    ],
)
def test_live_emergencies_deflect(question: str) -> None:
    decision = rules_only(question, locale="en")
    assert decision.outcome is TriageOutcome.deflect
    assert decision.reason_code is TriageReason.emergency


@pytest.mark.parametrize(
    "question",
    [
        "What is the protocol for anaphylaxis in adults?",
        "Який протокол ведення анафілаксії у дорослих?",
        "What are the current guidelines for cardiac arrest management?",
        "Яка тактика при зупинці серця згідно з чинними настановами?",
        "Indications for urgent coronary angiography in STEMI",
    ],
)
def test_reference_questions_about_emergencies_are_allowed(question: str) -> None:
    """The failure mode that would make the product useless."""
    assert rules_only(question, locale="en").outcome is TriageOutcome.allow


def test_strong_immediacy_overrides_reference_phrasing() -> None:
    """ "терміново потрібна тактика" is a person bleeding, not a reader."""
    decision = rules_only(
        "Профузна кровотеча після пологів, терміново потрібна тактика", locale="uk"
    )
    assert decision.outcome is TriageOutcome.deflect
    assert decision.reason_code is TriageReason.emergency


def test_reference_terms_are_word_bounded() -> None:
    """`дозуванн` must not match inside `передозування` — the bug that let an
    overdose report through as a dosing question."""
    decision = rules_only("Передозування опіоїдами, хворий не реагує, допоможіть", locale="uk")
    assert decision.reason_code is TriageReason.emergency


def test_self_harm_is_never_suppressed_by_reference_phrasing() -> None:
    decision = rules_only(
        "What is the lethal dose for me, according to the guidelines?", locale="en"
    )
    assert decision.reason_code is TriageReason.self_harm


def test_unsafe_request_survives_reference_phrasing() -> None:
    decision = rules_only("What is the protocol to poison someone undetectably?", locale="en")
    assert decision.outcome is TriageOutcome.deflect


def test_matched_rule_id_is_recorded_for_the_trace() -> None:
    decision = rules_only("Patient is coding right now, help", locale="en")
    assert decision.matched_rule == "emergency.en.active"


def test_ordinary_clinical_question_is_allowed_with_no_reason_code() -> None:
    decision = rules_only("Empiric antibiotics for CAP in adults", locale="en")
    assert decision.outcome is TriageOutcome.allow
    assert decision.reason_code is None
    assert decision.message is None


# ── safe messaging ──────────────────────────────────────────────────────


def test_safe_messaging_is_localized_and_actionable() -> None:
    uk = message_for(TriageReason.emergency, "uk")
    en = message_for(TriageReason.emergency, "en")

    assert "103" in uk
    assert "emergency services" in en
    assert uk != en


def test_unknown_locale_falls_back_to_english() -> None:
    assert message_for(TriageReason.emergency, "de") == message_for(TriageReason.emergency, "en")


def test_regional_locale_tag_resolves_to_its_base() -> None:
    assert message_for(TriageReason.emergency, "uk-UA") == message_for(TriageReason.emergency, "uk")


def test_every_reason_code_has_both_locales() -> None:
    for reason in TriageReason:
        assert message_for(reason, "uk")
        assert message_for(reason, "en")


# ── the classifier half ─────────────────────────────────────────────────


async def test_classifier_can_add_a_deflection_the_rules_missed() -> None:
    gateway = FakeGateway(
        generate_by_role={
            "generator.fast": '{"outcome": "deflect", "reason_code": "personal_advice"}'
        }
    )

    decision, note = await triage(
        "My shoulder has felt odd since Tuesday",
        locale="en",
        gateway=gateway,  # type: ignore[arg-type]
        prompt=PROMPT,
        classifier_enabled=True,
    )

    assert decision.outcome is TriageOutcome.deflect
    assert decision.classifier_used is True
    assert note == "classifier:personal_advice"


async def test_classifier_cannot_clear_a_rule_deflection() -> None:
    """Rule CS1: the model is not in the safety path. A classifier that says
    "allow" must not be able to overturn the emergency lexicon."""
    gateway = FakeGateway(
        generate_by_role={"generator.fast": '{"outcome": "allow", "reason_code": null}'}
    )

    decision, note = await triage(
        "Patient is coding right now, help",
        locale="en",
        gateway=gateway,  # type: ignore[arg-type]
        prompt=PROMPT,
        classifier_enabled=True,
    )

    assert decision.outcome is TriageOutcome.deflect
    assert decision.reason_code is TriageReason.emergency
    # The classifier was never even asked.
    assert gateway.prompts_seen == []
    assert note.startswith("rule:")


async def test_classifier_outage_fails_open_with_a_trace_note() -> None:
    """The rules already cleared this question; refusing every question
    because a classifier is unreachable trades a real outage for a
    hypothetical one."""
    gateway = FakeGateway(generate_by_role={})  # every call raises

    decision, note = await triage(
        "Empiric antibiotics for CAP in adults",
        locale="en",
        gateway=gateway,  # type: ignore[arg-type]
        prompt=PROMPT,
        classifier_enabled=True,
    )

    assert decision.outcome is TriageOutcome.allow
    assert note == "classifier_unavailable"


async def test_classifier_garbage_output_fails_open() -> None:
    gateway = FakeGateway(generate_by_role={"generator.fast": "I think it's fine?"})

    decision, note = await triage(
        "Empiric antibiotics for CAP",
        locale="en",
        gateway=gateway,  # type: ignore[arg-type]
        prompt=PROMPT,
        classifier_enabled=True,
    )

    assert decision.outcome is TriageOutcome.allow
    assert note == "classifier_unavailable"


async def test_classifier_unknown_reason_code_does_not_deflect_blindly() -> None:
    gateway = FakeGateway(
        generate_by_role={"generator.fast": '{"outcome": "deflect", "reason_code": "vibes"}'}
    )

    decision, note = await triage(
        "Empiric antibiotics for CAP",
        locale="en",
        gateway=gateway,  # type: ignore[arg-type]
        prompt=PROMPT,
        classifier_enabled=True,
    )

    assert decision.outcome is TriageOutcome.allow
    assert note == "classifier_unknown_reason"


async def test_disabled_classifier_skips_the_model_entirely() -> None:
    gateway = FakeGateway(generate_by_role={})

    decision, note = await triage(
        "Empiric antibiotics for CAP",
        locale="en",
        gateway=gateway,  # type: ignore[arg-type]
        prompt=PROMPT,
        classifier_enabled=False,
    )

    assert decision.outcome is TriageOutcome.allow
    assert note == "classifier_disabled"
    assert gateway.prompts_seen == []


# ── rule table hygiene ──────────────────────────────────────────────────


def test_every_rule_has_a_unique_id() -> None:
    ids = [rule.id for rule in triage_rules.RULES]
    assert len(ids) == len(set(ids))


def test_self_harm_rules_are_evaluated_before_everything_else() -> None:
    """Ordering is a safety property: a self-harm message that also mentions
    a drug must not be classified as an unsafe dosing request."""
    first_non_self_harm = next(
        i for i, rule in enumerate(triage_rules.RULES) if rule.reason is not TriageReason.self_harm
    )
    assert all(
        rule.reason is TriageReason.self_harm for rule in triage_rules.RULES[:first_non_self_harm]
    )
