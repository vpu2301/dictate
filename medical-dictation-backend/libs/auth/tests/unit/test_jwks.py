"""JWKS cache: TTL, refresh-on-miss, storm prevention, rate limit, rotation."""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import pytest

from auth.exceptions import JwksFetchError, KidNotFoundError
from auth.jwks import JwksCache

from ..conftest import ISSUER, JWKS_URL, FakeJwksServer, RSATestKey

pytestmark = pytest.mark.asyncio


async def test_first_call_fetches_and_returns_key(
    jwks_cache: JwksCache, jwks_server: FakeJwksServer, rsa_key_1: RSATestKey
) -> None:
    key = await jwks_cache.get_key(ISSUER, rsa_key_1.kid)
    assert key["kid"] == rsa_key_1.kid
    assert jwks_server.call_count == 1
    assert jwks_cache.metrics.refresh_attempts == 1
    assert jwks_cache.metrics.cache_misses == 1


async def test_second_call_hits_cache(
    jwks_cache: JwksCache, jwks_server: FakeJwksServer, rsa_key_1: RSATestKey
) -> None:
    await jwks_cache.get_key(ISSUER, rsa_key_1.kid)
    await jwks_cache.get_key(ISSUER, rsa_key_1.kid)
    assert jwks_server.call_count == 1
    assert jwks_cache.metrics.cache_hits == 1


async def test_unknown_issuer_raises_without_fetch(
    jwks_cache: JwksCache, jwks_server: FakeJwksServer
) -> None:
    with pytest.raises(KidNotFoundError, match="unknown issuer"):
        await jwks_cache.get_key("https://attacker.example/realm", "any-kid")
    assert jwks_server.call_count == 0


async def test_kid_not_in_jwks_refreshes_then_raises(
    jwks_cache: JwksCache, jwks_server: FakeJwksServer, rsa_key_1: RSATestKey
) -> None:
    # First, warm the cache.
    await jwks_cache.get_key(ISSUER, rsa_key_1.kid)
    assert jwks_server.call_count == 1

    # Request an unknown kid → triggers refresh; JWKS still doesn't have it.
    with pytest.raises(KidNotFoundError, match="after refresh"):
        await jwks_cache.get_key(ISSUER, "never-issued-this-kid")
    assert jwks_server.call_count == 2  # exactly one refresh attempted


async def test_kid_rotation_picked_up_after_refresh(
    jwks_server: FakeJwksServer, rsa_key_1: RSATestKey, rsa_key_2: RSATestKey
) -> None:
    """When the IdP rotates kids, the new kid is discovered via refresh."""
    transport = httpx.MockTransport(jwks_server.handler)
    client = httpx.AsyncClient(transport=transport, timeout=5.0)
    cache = JwksCache(
        issuer_to_url={ISSUER: JWKS_URL},
        refresh_rate_limit_seconds=0,  # disable rate limit for this test
        http_client=client,
    )
    try:
        # Warm cache with key 1
        await cache.get_key(ISSUER, rsa_key_1.kid)
        assert jwks_server.call_count == 1

        # IdP rotates → now serves key 2
        jwks_server.replace_keys([rsa_key_2.public_jwk])

        # Asking for key 2 triggers refresh and resolves
        key = await cache.get_key(ISSUER, rsa_key_2.kid)
        assert key["kid"] == rsa_key_2.kid
        assert jwks_server.call_count == 2
    finally:
        await cache.aclose()


async def test_rate_limit_suppresses_repeated_refreshes(
    jwks_cache: JwksCache, jwks_server: FakeJwksServer, rsa_key_1: RSATestKey
) -> None:
    """A second request for an unknown kid within the rate-limit window
    must NOT trigger another HTTP fetch."""
    await jwks_cache.get_key(ISSUER, rsa_key_1.kid)
    initial = jwks_server.call_count  # = 1

    # First miss: triggers refresh (count→2), but kid still missing.
    with pytest.raises(KidNotFoundError):
        await jwks_cache.get_key(ISSUER, "unknown-kid-x")
    assert jwks_server.call_count == initial + 1

    # Second miss within 5 s: rate-limit suppresses refresh.
    with pytest.raises(KidNotFoundError, match="rate limit"):
        await jwks_cache.get_key(ISSUER, "unknown-kid-y")
    assert jwks_server.call_count == initial + 1  # unchanged
    assert jwks_cache.metrics.rate_limited_refreshes >= 1


async def test_storm_prevention_one_fetch_for_100_concurrent_misses(
    jwks_server: FakeJwksServer, rsa_key_1: RSATestKey
) -> None:
    """100 concurrent verifies with the SAME new kid produce exactly one fetch.

    Even though the kid is unknown (and the refresh will not resolve it),
    only one HTTP call may be made — the per-issuer lock funnels them.
    """
    # Make the JWKS endpoint slow so that all 100 coroutines pile up on the lock.
    slow_release = asyncio.Event()
    real_handler = jwks_server.handler

    def slow_handler(request: httpx.Request) -> httpx.Response:
        # Block first call until slow_release is set; subsequent calls go fast.
        return real_handler(request)

    transport = httpx.MockTransport(slow_handler)
    client = httpx.AsyncClient(transport=transport, timeout=5.0)
    cache = JwksCache(
        issuer_to_url={ISSUER: JWKS_URL},
        # Default 5 s rate-limit is what *enables* storm prevention: after the
        # first fetch leaves the cache without the requested kid, follow-up
        # lookups within 5 s are rejected without a network call.
        refresh_rate_limit_seconds=5,
        http_client=client,
    )
    try:

        async def fetch_unknown() -> None:
            with contextlib.suppress(KidNotFoundError):
                await cache.get_key(ISSUER, "storm-test-kid")

        # Note: with the in-process MockTransport, calls return effectively
        # synchronously; the per-issuer lock still serialises them so only
        # the first one performs a real fetch.
        await asyncio.gather(*[fetch_unknown() for _ in range(100)])

        # The first fetch refreshes; remaining 99 hit the rate-limit branch
        # and never re-fetch.
        assert jwks_server.call_count == 1, (
            f"expected exactly 1 JWKS fetch under storm; got {jwks_server.call_count}"
        )
        assert cache.metrics.rate_limited_refreshes >= 99
    finally:
        await cache.aclose()
        slow_release.set()


async def test_fetch_failure_raises_jwks_fetch_error(rsa_key_1: RSATestKey) -> None:
    """HTTP 500 from the JWKS endpoint must surface as JwksFetchError."""

    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    transport = httpx.MockTransport(boom)
    client = httpx.AsyncClient(transport=transport, timeout=5.0)
    cache = JwksCache(issuer_to_url={ISSUER: JWKS_URL}, http_client=client)
    try:
        with pytest.raises(JwksFetchError):
            await cache.get_key(ISSUER, rsa_key_1.kid)
    finally:
        await cache.aclose()


async def test_ttl_triggers_refresh(rsa_key_1: RSATestKey, jwks_server: FakeJwksServer) -> None:
    """When the cached document exceeds TTL, the next get_key refreshes."""
    fake_now = [1_000_000.0]

    def clock() -> float:
        return fake_now[0]

    transport = httpx.MockTransport(jwks_server.handler)
    client = httpx.AsyncClient(transport=transport, timeout=5.0)
    cache = JwksCache(
        issuer_to_url={ISSUER: JWKS_URL},
        ttl_seconds=300,
        refresh_rate_limit_seconds=0,
        http_client=client,
        clock=clock,
    )
    try:
        await cache.get_key(ISSUER, rsa_key_1.kid)
        assert jwks_server.call_count == 1

        # Advance past TTL.
        fake_now[0] += 301
        await cache.get_key(ISSUER, rsa_key_1.kid)
        assert jwks_server.call_count == 2
    finally:
        await cache.aclose()
