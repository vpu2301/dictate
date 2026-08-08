"""ClinicalIntent v1 (rule RC3).

Concepts are de-identified by construction: free text never enters a concept,
only extracted clinical terms and codes. Web queries are built exclusively
from these concepts (rule QS1).
"""

from __future__ import annotations

from enum import StrEnum

from .common import Coding, ContractVersion, StrictModel


class QuestionType(StrEnum):
    diagnosis = "diagnosis"
    therapy = "therapy"
    dosing = "dosing"
    interaction = "interaction"
    contraindication = "contraindication"
    prognosis = "prognosis"
    etiology = "etiology"
    prevention = "prevention"
    other = "other"


class ClinicalConcept(StrictModel):
    text: str
    coding: list[Coding] = []


class ClinicalIntent(StrictModel):
    question_type: QuestionType
    concepts: list[ClinicalConcept] = []
    # PICO-ish population qualifier ("elderly", "pregnancy", "CKD stage 4").
    population: str | None = None
    negations: list[str] = []
    contract_version: ContractVersion = "1.0"
