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

# ── anamnesis ───────────────────────────────────────────────────────
ANAMNESIS_UPDATED: Final = "anamnesis.updated"

# ── privacy (DSAR / erasure) ────────────────────────────────────────
PRIVACY_DSAR_REQUESTED: Final = "privacy.dsar_requested"
PRIVACY_ERASURE_SCHEDULED: Final = "privacy.erasure_scheduled"
