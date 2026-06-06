"""Read-purpose enum required on full-content GET by non-authors.

Captured into audit on every read so DPO + clinical-content-lead can
audit access patterns.
"""

from __future__ import annotations

from enum import StrEnum


class ReadPurpose(StrEnum):
    CLINICAL_CONTINUITY = "clinical_continuity"
    AUDIT = "audit"
    LEGAL = "legal"
    QA_REVIEW = "qa_review"
    CONSULTATION = "consultation"
