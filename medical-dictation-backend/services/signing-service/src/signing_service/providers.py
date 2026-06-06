"""Provider registry — instantiates concrete providers from settings
and exposes a single ``get_provider(name)`` lookup.

The mock provider is loaded only when ``enable_mock_provider`` is set
in config AND the environment isn't production. The mock's own
constructor refuses production as a defence-in-depth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from medical_kep import MockProvider, ProviderName, SigningProvider
from medical_kep.diia_provider import DiiaConfig, DiiaProvider
from medical_kep.iit_provider import IitConfig, IitProvider

from .config import settings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ProviderRegistry:
    providers: dict[ProviderName, SigningProvider]

    def get(self, name: ProviderName) -> SigningProvider:
        try:
            return self.providers[name]
        except KeyError as exc:
            raise ValueError(f"provider {name!r} not configured") from exc

    async def aclose(self) -> None:
        for p in self.providers.values():
            try:
                await p.aclose()
            except Exception:  # noqa: BLE001
                logger.warning("provider.close_failed: %s", p.__class__.__name__)


def build_registry() -> ProviderRegistry:
    providers: dict[ProviderName, SigningProvider] = {}

    if settings.diia_base_url and settings.diia_api_token:
        providers[ProviderName.DIIA] = DiiaProvider(
            DiiaConfig(
                base_url=settings.diia_base_url,
                api_token=settings.diia_api_token,
            )
        )
    else:
        logger.info("provider.diia.disabled (no base_url/token)")

    if settings.iit_helper_health_url:
        providers[ProviderName.IIT] = IitProvider(
            IitConfig(
                helper_health_url=settings.iit_helper_health_url,
                callback_hmac_key=bytes.fromhex(settings.iit_callback_hmac_key_hex),
            )
        )
    else:
        logger.info("provider.iit.disabled (no helper_health_url)")

    if settings.enable_mock_provider:
        # The mock's constructor will refuse production.
        providers[ProviderName.MOCK] = MockProvider(
            environment=settings.environment,
        )

    return ProviderRegistry(providers=providers)
