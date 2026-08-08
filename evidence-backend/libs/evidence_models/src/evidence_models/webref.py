"""Web source references produced by the Quick-Search web agent (S04 consumer)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from .common import StrictModel


class WebTrustTier(StrEnum):
    # S04 additive: WHO/ECDC/EMA are intergovernmental, not any one state's
    # government — collapsing them onto `government` lost the distinction the
    # allowlist review actually cares about.
    international_organization = "international_organization"
    government = "government"
    professional_society = "professional_society"
    guideline_registry = "guideline_registry"
    journal = "journal"
    other = "other"


class WebSourceRef(StrictModel):
    url: str
    domain: str
    trust_tier: WebTrustTier
    accessed_at: datetime
    # Object key of the cached page snapshot; None until the fetch proxy stored one.
    snapshot_ref: str | None = None
