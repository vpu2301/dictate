"""S11 step 05 — the erasure/DSAR fan-out package.

`fanout.FANOUT` is the single source of truth for "where patient data
lives". The DSAR export (step 06) renders it, the erasure engine
(step 07) destroys along it, and `scripts/ci/
check_erasure_fanout_coverage.py` fails the build when a new
patient-linked table isn't registered here.
"""

from .fanout import (
    FANOUT,
    KNOWN_NON_PHI,
    SOFT_LINKED_PHI,
    Artifact,
    Erasability,
    ExportItem,
    FanoutInventory,
    enumerate_patient,
)

__all__ = [
    "FANOUT",
    "KNOWN_NON_PHI",
    "SOFT_LINKED_PHI",
    "Artifact",
    "Erasability",
    "ExportItem",
    "FanoutInventory",
    "enumerate_patient",
]
