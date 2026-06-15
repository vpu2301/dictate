"""Provider-health tracking record persisted in
``signing_provider_health`` table (sprint-09 migration 0021)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from medical_kep.provider import ProviderName


@dataclass(slots=True)
class ProviderHealth:
    provider: ProviderName
    healthy: bool
    last_check_at: datetime
    consecutive_failures: int
    last_error: str | None = None

    @classmethod
    def fresh(cls, provider: ProviderName) -> ProviderHealth:
        return cls(
            provider=provider,
            healthy=True,
            last_check_at=datetime.now(UTC),
            consecutive_failures=0,
            last_error=None,
        )
