"""Async JWKS cache with TTL, refresh-on-miss, and storm prevention.

Contract:
- Multiple issuers are supported; each ``(issuer → JWKS URL)`` pair is
  registered at construction.
- ``get_key(issuer, kid)`` returns the matching JWK or raises
  :class:`auth.exceptions.KidNotFoundError`.
- On a cache miss the document is fetched synchronously on the first call
  to encounter the miss (no background task — behaviour is deterministic).
- A per-issuer ``asyncio.Lock`` serialises refresh attempts so that 100
  concurrent verifies with the same unknown ``kid`` produce exactly one
  HTTP fetch.
- A rate-limit (default 5 s) prevents a forged token with a random ``kid``
  from causing a JWKS-fetch storm against the IdP.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from .exceptions import JwksFetchError, KidNotFoundError


@dataclass(slots=True)
class _IssuerState:
    """In-memory snapshot of a single issuer's JWKS document.

    ``fetched_at`` drives the TTL: when the document is older than
    ``ttl_seconds`` the next access refreshes it.

    ``last_miss_refresh_at`` drives the storm-prevention rate-limit: it is
    set ONLY when we refresh because a queried kid was missing. The initial
    cache-fill and TTL-driven refreshes do not bump it, so a legitimate
    rotation (new kid arrives, cache is fresh but doesn't have it) is
    allowed one refresh; subsequent misses within ``refresh_rate_limit_seconds``
    are rejected without an HTTP call.
    """

    jwks: dict[str, Any]
    fetched_at: float
    last_miss_refresh_at: float = 0.0


@dataclass(slots=True)
class JwksMetrics:
    """Counters exposed for observability (wired in Day 9)."""

    cache_hits: int = 0
    cache_misses: int = 0
    refresh_attempts: int = 0
    refresh_failures: int = 0
    rate_limited_refreshes: int = 0


class JwksCache:
    """Async JWKS document cache, keyed by issuer.

    Parameters
    ----------
    issuer_to_url
        Mapping ``{issuer: jwks_url}``. Issuers not in this mapping are
        rejected before any HTTP call — defence in depth against typo-ed
        or attacker-supplied ``iss`` claims.
    ttl_seconds
        How long a JWKS document is considered fresh before a refresh is
        triggered on the next access. Default 300 s.
    refresh_rate_limit_seconds
        Minimum interval between refresh attempts per issuer. Default 5 s.
    http_client
        Optional pre-configured ``httpx.AsyncClient``. Useful for tests
        that wire a custom ``MockTransport``. If omitted, a client with a
        5 s timeout is created internally.
    clock
        Monotonic clock callable. Tests override to simulate time passing.
    """

    def __init__(
        self,
        *,
        issuer_to_url: Mapping[str, str],
        ttl_seconds: int = 300,
        refresh_rate_limit_seconds: int = 5,
        http_client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not issuer_to_url:
            raise ValueError("issuer_to_url must have at least one entry")
        self._issuer_to_url: dict[str, str] = dict(issuer_to_url)
        self._ttl = ttl_seconds
        self._rate_limit = refresh_rate_limit_seconds
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=5.0)
        self._clock = clock or time.monotonic
        self._state: dict[str, _IssuerState] = {}
        self._locks: dict[str, asyncio.Lock] = {iss: asyncio.Lock() for iss in self._issuer_to_url}
        self.metrics = JwksMetrics()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_key(self, issuer: str, kid: str) -> dict[str, Any]:
        """Return the JWK matching ``kid`` for ``issuer``, refreshing if needed.

        Raises:
            KidNotFoundError: ``issuer`` is unknown, or after refresh the
                JWKS still does not contain ``kid``, or a refresh was
                suppressed by the rate-limit and the cache miss persists.
            JwksFetchError: the JWKS endpoint returned an error or could
                not be parsed as JSON.
        """
        if issuer not in self._issuer_to_url:
            raise KidNotFoundError(f"unknown issuer: {issuer!r}")

        # Fast path: cached and fresh and contains the kid.
        state = self._state.get(issuer)
        now = self._clock()
        if state is not None and (now - state.fetched_at) <= self._ttl:
            key = _find_key(state.jwks, kid)
            if key is not None:
                self.metrics.cache_hits += 1
                return key

        # Slow path: take the per-issuer lock, re-check, then maybe fetch.
        async with self._locks[issuer]:
            state = self._state.get(issuer)
            now = self._clock()
            fresh = state is not None and (now - state.fetched_at) <= self._ttl

            if fresh:
                assert state is not None
                key = _find_key(state.jwks, kid)
                if key is not None:
                    # Another waiter refreshed while we were queued.
                    self.metrics.cache_hits += 1
                    return key

                # Fresh cache, kid missing → apply the rate-limit so a
                # forged token with a random kid cannot induce a JWKS-fetch
                # storm. The rate-limit clock starts at the previous *failed*
                # lookup, so a legitimate first miss is always allowed.
                if (now - state.last_miss_refresh_at) < self._rate_limit:
                    self.metrics.rate_limited_refreshes += 1
                    raise KidNotFoundError(
                        f"kid {kid!r} not in JWKS for issuer {issuer!r}; "
                        f"refresh suppressed by rate limit"
                    )
            # else: state is None or stale → always fetch (TTL refresh / first fill).

            # Fetch (this is the only place an HTTP call happens).
            self.metrics.cache_misses += 1
            self.metrics.refresh_attempts += 1
            url = self._issuer_to_url[issuer]
            attempt_at = self._clock()
            try:
                resp = await self._client.get(url)
                resp.raise_for_status()
                jwks_doc = resp.json()
            except Exception as exc:
                self.metrics.refresh_failures += 1
                raise JwksFetchError(f"failed to fetch JWKS from {url}: {exc}") from exc

            # Build new state. Preserve any previous miss timestamp so a
            # successful TTL refresh doesn't unblock rate-limited misses.
            new_state = _IssuerState(jwks=jwks_doc, fetched_at=attempt_at)
            if state is not None:
                new_state.last_miss_refresh_at = state.last_miss_refresh_at

            key = _find_key(jwks_doc, kid)
            if key is None:
                # The fetch happened but the requested kid still isn't there.
                # Stamp the miss-refresh time so the next caller within the
                # rate-limit window is rejected without re-fetching.
                new_state.last_miss_refresh_at = attempt_at
                self._state[issuer] = new_state
                raise KidNotFoundError(
                    f"kid {kid!r} not in JWKS for issuer {issuer!r} after refresh"
                )

            self._state[issuer] = new_state
            return key


def _find_key(jwks: Mapping[str, Any], kid: str) -> dict[str, Any] | None:
    keys = jwks.get("keys", [])
    if not isinstance(keys, list):
        return None
    for k in keys:
        if isinstance(k, dict) and k.get("kid") == kid:
            return k
    return None
