"""Provider selection logic — sprint-09 day-5.

The signing-service consults this module before every session-init.

Rules:
- Default provider: Дія (mobile flow has the highest pilot adoption).
- If Дія is unhealthy: fall back to ІІТ; FE shows only ІІТ.
- If both are unhealthy: return no available providers — FE shows
  "Signing temporarily unavailable, retry later".
- User-selected provider always honoured if that provider is healthy.
- The mock is *never* selected outside dev/test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from medical_kep.provider import ProviderName


@dataclass(slots=True)
class ProviderAvailability:
    available: list[ProviderName]
    default: ProviderName | None
    unhealthy: list[ProviderName]
    chosen: ProviderName | None = None


def select_providers(
    *,
    health: Mapping[ProviderName, bool],
    user_choice: ProviderName | None = None,
    allow_mock: bool = False,
) -> ProviderAvailability:
    healthy: list[ProviderName] = []
    unhealthy: list[ProviderName] = []
    for p in (ProviderName.DIIA, ProviderName.IIT):
        if health.get(p, False):
            healthy.append(p)
        else:
            unhealthy.append(p)
    if allow_mock and health.get(ProviderName.MOCK, True):
        healthy.append(ProviderName.MOCK)

    default: ProviderName | None
    if ProviderName.DIIA in healthy:
        default = ProviderName.DIIA
    elif ProviderName.IIT in healthy:
        default = ProviderName.IIT
    elif healthy:
        default = healthy[0]
    else:
        default = None

    chosen: ProviderName | None = None
    if user_choice is not None and user_choice in healthy:
        chosen = user_choice
    elif default is not None:
        chosen = default

    return ProviderAvailability(
        available=healthy,
        default=default,
        unhealthy=unhealthy,
        chosen=chosen,
    )
