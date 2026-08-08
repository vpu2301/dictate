"""Shared primitives for all evidence contracts.

Every wire model derives from StrictModel (extra="forbid", rule E10).
Enum values are lowercase snake and final once released (v2 path for changes).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

# Semantic contract version, "MAJOR.MINOR". Additive change = minor bump;
# breaking change = major bump + new contract file + ADR (rule RC1).
ContractVersion = Annotated[str, Field(pattern=r"^\d+\.\d+$")]

CONTRACTS_V1: str = "1.0"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Coding(StrictModel):
    """A code in a terminology system (ICD-10, ATC, SNOMED, LOINC, ...)."""

    system: str
    code: str
    display: str | None = None


class NormalizedQuantity(StrictModel):
    """A quantity converted to the canonical unit for its dimension."""

    value: float
    unit: str


class Quantity(StrictModel):
    value: float
    unit: str
    normalized: NormalizedQuantity | None = None
