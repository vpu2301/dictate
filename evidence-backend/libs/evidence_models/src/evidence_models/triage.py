"""Triage decision contract (S04 intake stage, rule CS2).

Triage is the first stage of every answer pipeline: it decides whether the
question may be answered at all. A deflection is a *first-class answer state*
(AnswerStatus.deflected), not an error — the clinician gets safe messaging,
not a stack trace.

Reason codes are stable wire values: the SPA renders per-code guidance and the
observability layer counts deflections by code.
"""

from __future__ import annotations

from enum import StrEnum

from .common import ContractVersion, StrictModel


class TriageOutcome(StrEnum):
    allow = "allow"
    deflect = "deflect"


class TriageReason(StrEnum):
    """Why a question was deflected. Final for v1; new codes are additive."""

    # Time-critical presentation described as happening now — the answer is
    # "call emergency services", never a literature summary.
    emergency = "emergency"
    # Self-harm / suicidal ideation phrasing.
    self_harm = "self_harm"
    # A layperson asking for personal medical advice (product is clinician-facing).
    personal_advice = "personal_advice"
    # Not a clinical question at all (chit-chat, prompt probing, admin request).
    out_of_scope = "out_of_scope"
    # Request for something the product must not produce (dosing for harm, etc.).
    unsafe_request = "unsafe_request"


class TriageDecision(StrictModel):
    outcome: TriageOutcome
    reason_code: TriageReason | None = None
    # Safe messaging in the question's locale — rendered verbatim by the SPA.
    message: str | None = None
    # Rule id that fired ("emergency.uk.cardiac_arrest"), or None for a
    # classifier-only decision. Deterministic rules win over the classifier.
    matched_rule: str | None = None
    classifier_used: bool = False
    contract_version: ContractVersion = "1.0"
