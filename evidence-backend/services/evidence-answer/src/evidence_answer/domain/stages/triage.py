"""Triage stage: deterministic rules, then the `generator.fast` classifier.

Order is the safety property (rule CS1): the rule list runs first and its
verdict is final. The classifier only sees questions the rules cleared, and it
can only *add* a deflection — it can never clear one. A model that could
overturn the emergency lexicon would be a model in the safety path.

Safe messaging is authored here, in both locales, per reason code. It is
never generated: a deflection message is a clinical artifact, and a model
paraphrasing "call emergency services" is exactly the kind of fluency this
product is built to avoid.
"""

from __future__ import annotations

import json
import logging

from evidence_answer.domain.stages import triage_rules
from evidence_models import TriageDecision, TriageOutcome, TriageReason
from models import GatewayError, ModelGatewayClient

logger = logging.getLogger(__name__)

CLASSIFIER_ROLE = "generator.fast"

# Safe messaging, per reason code, per locale. uk is the default locale of the
# platform; en is the second supported answer language (rule FE4).
_MESSAGES: dict[TriageReason, dict[str, str]] = {
    TriageReason.emergency: {
        "uk": (
            "Це схоже на невідкладний стан. Негайно викличте екстрену медичну "
            "допомогу (103) і дійте за чинним протоколом реанімації вашого "
            "закладу. Ця система не призначена для допомоги в реальному часі "
            "під час невідкладних станів."
        ),
        "en": (
            "This reads as an emergency happening now. Call emergency services "
            "immediately and follow your institution's resuscitation protocol. "
            "This system is not intended for real-time emergency support."
        ),
    },
    TriageReason.self_harm: {
        "uk": (
            "Якщо ви або хтось поруч у небезпеці, зателефонуйте 103 або на "
            "Лінію запобігання самогубствам 7333 (безкоштовно, цілодобово). "
            "Ця система не може надати допомогу в кризовій ситуації."
        ),
        "en": (
            "If you or someone near you is in danger, call your local emergency "
            "number or a suicide prevention line now. This system cannot provide "
            "crisis support."
        ),
    },
    TriageReason.personal_advice: {
        "uk": (
            "Ця система призначена для медичних працівників і не дає порад "
            "щодо власного здоров'я. Зверніться, будь ласка, до свого лікаря."
        ),
        "en": (
            "This system is built for clinicians and does not give personal "
            "medical advice. Please speak with your own doctor."
        ),
    },
    TriageReason.out_of_scope: {
        "uk": "Це не клінічне запитання — я відповідаю лише на клінічні запити.",
        "en": "That is not a clinical question — I only answer clinical queries.",
    },
    TriageReason.unsafe_request: {
        "uk": "Я не можу відповісти на цей запит.",
        "en": "I cannot answer this request.",
    },
}

_CLASSIFIER_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": ["allow", "deflect"]},
        "reason_code": {
            "type": ["string", "null"],
            "enum": [*(r.value for r in TriageReason), None],
        },
    },
    "required": ["outcome", "reason_code"],
    "additionalProperties": False,
}


def message_for(reason: TriageReason, locale: str) -> str:
    localized = _MESSAGES[reason]
    return localized.get(locale.split("-")[0].casefold(), localized["en"])


def rules_only(question: str, *, locale: str) -> TriageDecision:
    """The deterministic half — the part the 100% emergency gate measures."""
    rule = triage_rules.match(question)
    if rule is None:
        return TriageDecision(outcome=TriageOutcome.allow)
    return TriageDecision(
        outcome=TriageOutcome.deflect,
        reason_code=rule.reason,
        message=message_for(rule.reason, locale),
        matched_rule=rule.id,
    )


async def triage(
    question: str,
    *,
    locale: str,
    gateway: ModelGatewayClient,
    prompt: str,
    classifier_enabled: bool,
) -> tuple[TriageDecision, str]:
    """Returns (decision, trace_note)."""
    decision = rules_only(question, locale=locale)
    if decision.outcome is TriageOutcome.deflect:
        return decision, f"rule:{decision.matched_rule}"
    if not classifier_enabled:
        return decision, "classifier_disabled"

    try:
        result = await gateway.generate(
            CLASSIFIER_ROLE,
            prompt.format(question=question),
            max_tokens=64,
            temperature=0.0,
            json_schema=_CLASSIFIER_SCHEMA,
        )
        payload = json.loads(result.text)
    except (GatewayError, json.JSONDecodeError, ValueError) as exc:
        # Fail-open: the rules already cleared this question, and taking the
        # product down because a classifier is unreachable trades a real
        # outage for a hypothetical one.
        logger.warning("triage.classifier_unavailable", extra={"error": type(exc).__name__})
        return decision, "classifier_unavailable"

    if payload.get("outcome") != "deflect":
        return decision, "classifier_allow"
    raw_reason = payload.get("reason_code")
    try:
        reason = TriageReason(raw_reason)
    except ValueError:
        logger.warning("triage.classifier_unknown_reason")
        return decision, "classifier_unknown_reason"
    return (
        TriageDecision(
            outcome=TriageOutcome.deflect,
            reason_code=reason,
            message=message_for(reason, locale),
            classifier_used=True,
        ),
        f"classifier:{reason.value}",
    )
