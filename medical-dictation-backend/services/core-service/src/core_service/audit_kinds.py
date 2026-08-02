"""Audit kinds emitted by core-service. See docs/audit/event-kinds.md."""

from __future__ import annotations

from typing import Final

# ── patients ────────────────────────────────────────────────────────
PATIENT_CREATED: Final = "patient.created"
PATIENT_UPDATED: Final = "patient.updated"
PATIENT_VIEWED: Final = "patient.viewed"

# ── break-glass (S15) ───────────────────────────────────────────────
# Emitted per read of a patient record served under a patient-kind
# grant, `sec` severity — same kind report-service emits for report
# grants, so "every break-glass read" stays one query over the chain.
PHI_ACCESS_USED: Final = "phi_access.used"

# ── encounters ──────────────────────────────────────────────────────
ENCOUNTER_CREATED: Final = "encounter.created"
# Lifecycle (0058). One kind per clinical action, not a generic
# `encounter.updated` — "who ended this visit and when" has to be answerable
# from the audit chain alone.
ENCOUNTER_STARTED: Final = "encounter.started"
ENCOUNTER_PAUSED: Final = "encounter.paused"
ENCOUNTER_RESUMED: Final = "encounter.resumed"
ENCOUNTER_COMPLETED: Final = "encounter.completed"
ENCOUNTER_CANCELLED: Final = "encounter.cancelled"

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
# S11 step 06 — DSAR export engine.
DSAR_EXPORT_COMPLETED: Final = "dsar.export.completed"
DSAR_EXPORT_FAILED: Final = "dsar.export.failed"
DSAR_DOWNLOAD_LINK_ISSUED: Final = "dsar.download.link_issued"
DSAR_PACKAGE_DOWNLOADED: Final = "dsar.package.downloaded"
# S11 step 07 — the erasure engine.
ERASURE_EXECUTING: Final = "erasure.executing"
ERASURE_ARTIFACT_DESTROYED: Final = "erasure.artifact_destroyed"
ERASURE_EXECUTED: Final = "erasure.executed"
# Reserved since sprint 03 (0007's DELETE comment); real as of step 07.
ASR_AUDIO_DELETED: Final = "asr.audio_deleted"
