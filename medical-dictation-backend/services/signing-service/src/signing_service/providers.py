"""Provider registry — instantiates concrete providers from settings
and exposes a single ``get_provider(name)`` lookup.

The mock provider is loaded only when ``enable_mock_provider`` is set
in config AND the environment isn't production. The mock's own
constructor refuses production as a defence-in-depth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from medical_kep import (
    DevPasswordProvider,
    FileKeyProvider,
    InlineSigner,
    MockProvider,
    ProviderName,
    SigningProvider,
    UapkiBackend,
    UapkiConfig,
)
from medical_kep.diia_provider import DiiaConfig, DiiaProvider
from medical_kep.iit_provider import IitConfig, IitProvider

from .config import settings
from .keycloak_password import KeycloakPasswordVerifier

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ProviderRegistry:
    providers: dict[ProviderName, SigningProvider]
    inline: dict[ProviderName, InlineSigner] = field(default_factory=dict)

    def get(self, name: ProviderName) -> SigningProvider:
        try:
            return self.providers[name]
        except KeyError as exc:
            raise ValueError(f"provider {name!r} not configured") from exc

    def get_inline(self, name: ProviderName) -> InlineSigner:
        try:
            return self.inline[name]
        except KeyError as exc:
            raise ValueError(f"inline provider {name!r} not configured") from exc

    async def aclose(self) -> None:
        for p in [*self.providers.values(), *self.inline.values()]:
            try:
                await p.aclose()
            except Exception:  # noqa: BLE001
                logger.warning("provider.close_failed: %s", p.__class__.__name__)


def build_registry() -> ProviderRegistry:
    providers: dict[ProviderName, SigningProvider] = {}
    inline: dict[ProviderName, InlineSigner] = {}

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

    # ── Inline signers ───────────────────────────────────────────────

    uapki_lib_present = (settings.uapki_lib_dir / "libuapki.so.2").exists() or (
        settings.uapki_lib_dir / "libuapki.so"
    ).exists()
    if uapki_lib_present:
        settings.uapki_cert_cache_dir.mkdir(parents=True, exist_ok=True)
        settings.uapki_crl_cache_dir.mkdir(parents=True, exist_ok=True)
        inline[ProviderName.FILE_KEY] = FileKeyProvider(
            backend=UapkiBackend(
                UapkiConfig(
                    lib_dir=settings.uapki_lib_dir,
                    cert_cache_dir=settings.uapki_cert_cache_dir,
                    crl_cache_dir=settings.uapki_crl_cache_dir,
                    tsp_url=settings.uapki_tsp_url or None,
                    offline=settings.uapki_offline,
                )
            )
        )
    else:
        logger.info("provider.file_key.disabled (no UAPKI libs at %s)", settings.uapki_lib_dir)

    if settings.enable_dev_password_provider:
        # Config already rejected this flag in production; the provider's
        # own constructor refuses production as defence-in-depth.
        inline[ProviderName.DEV_PASSWORD] = DevPasswordProvider(
            password_verifier=KeycloakPasswordVerifier(),
            environment=settings.environment,
        )
        logger.warning(
            "provider.dev_password.ENABLED — development-only scaffold; "
            "envelopes are level='dev' and never legally signed"
        )

    return ProviderRegistry(providers=providers, inline=inline)
