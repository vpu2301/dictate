"""Dependency wiring for the template service.

Kept separate from main.py so router modules can import the dependencies
without triggering a circular import at module-load time.
"""

from auth import JwksCache, build_current_user

from .config import settings

_jwks_cache = JwksCache(issuer_to_url={settings.auth_issuer: settings.auth_jwks_url})

current_user = build_current_user(
    jwks_cache=_jwks_cache,
    expected_audience=settings.auth_audience,
    expected_issuer=settings.auth_issuer,
    clock_skew_seconds=settings.auth_clock_skew_seconds,
)


async def close_auth() -> None:
    """Close the JWKS HTTP client on shutdown."""
    await _jwks_cache.aclose()
