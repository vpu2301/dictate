"""Audit kinds emitted by core-service. See docs/audit/event-kinds.md."""

from __future__ import annotations

from typing import Final

# ── patients ────────────────────────────────────────────────────────
PATIENT_CREATED: Final = "patient.created"
PATIENT_UPDATED: Final = "patient.updated"
PATIENT_VIEWED: Final = "patient.viewed"

# ── encounters ──────────────────────────────────────────────────────
ENCOUNTER_CREATED: Final = "encounter.created"

# ── clinical notes ──────────────────────────────────────────────────
NOTE_CREATED: Final = "note.created"
NOTE_UPDATED: Final = "note.updated"
NOTE_SIGNED: Final = "note.signed"

# ── consents ────────────────────────────────────────────────────────
CONSENT_GRANTED: Final = "consent.granted"
CONSENT_WITHDRAWN: Final = "consent.withdrawn"
CONSENT_SIGNED: Final = "consent.signed"

# ── anamnesis ───────────────────────────────────────────────────────
ANAMNESIS_UPDATED: Final = "anamnesis.updated"

# ── privacy (DSAR / erasure) ────────────────────────────────────────
PRIVACY_DSAR_REQUESTED: Final = "privacy.dsar_requested"
# S11 step 04 — the two-person workflow. `privacy.erasure_scheduled`
# (S11-M2) is superseded: requests now start at `requested` and the
# schedule is set at approval.
PRIVACY_ERASURE_REQUESTED: Final = "privacy.erasure_requested"
PRIVACY_ERASURE_REVIEWED: Final = "privacy.erasure_reviewed"
PRIVACY_ERASURE_APPROVED: Final = "privacy.erasure_approved"
PRIVACY_ERASURE_REJECTED: Final = "privacy.erasure_rejected"
