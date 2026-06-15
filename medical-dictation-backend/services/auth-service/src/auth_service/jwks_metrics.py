"""OTel instrumentation for ``libs/auth``'s :class:`JwksCache`.

``JwksCache`` keeps pure in-memory counters on its ``.metrics`` field so the
library stays free of any observability dependency (it is a leaf-ish lib).
This module lives in the *service* — which already depends on OpenTelemetry —
and bridges those counters to OTel **observable** counters: on every metric
collection the callbacks read the current cumulative values off the cache.

The metric names match the ``JwksCacheHitRatioLow`` Prometheus alert authored
in Sprint 02 Day 9 (``infra/prometheus/rules/sprint-02-auth-audit.yml``), which
until now had no series feeding it.
"""

from __future__ import annotations

from collections.abc import Iterable

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Meter, Observation

from auth import JwksCache


def instrument_jwks_cache(cache: JwksCache, *, meter: Meter | None = None) -> None:
    """Register OTel observable counters that report ``cache.metrics``.

    Called once from ``build_state`` at lifespan startup. The counters are
    cumulative (monotonic), so an observable counter is the correct
    instrument — each collection reports the latest absolute value of the
    in-memory counter.

    The meter is resolved at call time (not import time) so it binds to the
    global ``MeterProvider`` configured during startup; tests may inject an
    SDK-backed ``meter`` to read the series back.
    """
    m = meter or metrics.get_meter("mdx.auth.jwks")

    def _hits(_: CallbackOptions) -> Iterable[Observation]:
        return (Observation(cache.metrics.cache_hits),)

    def _misses(_: CallbackOptions) -> Iterable[Observation]:
        return (Observation(cache.metrics.cache_misses),)

    def _refresh_attempts(_: CallbackOptions) -> Iterable[Observation]:
        return (Observation(cache.metrics.refresh_attempts),)

    def _refresh_failures(_: CallbackOptions) -> Iterable[Observation]:
        return (Observation(cache.metrics.refresh_failures),)

    def _rate_limited(_: CallbackOptions) -> Iterable[Observation]:
        return (Observation(cache.metrics.rate_limited_refreshes),)

    m.create_observable_counter(
        "mdx_jwks_cache_hits_total",
        callbacks=[_hits],
        description="JWKS cache hits (kid served from a fresh cached document)",
        unit="1",
    )
    m.create_observable_counter(
        "mdx_jwks_cache_misses_total",
        callbacks=[_misses],
        description="JWKS cache misses that triggered an HTTP fetch",
        unit="1",
    )
    m.create_observable_counter(
        "mdx_jwks_refresh_attempts_total",
        callbacks=[_refresh_attempts],
        description="JWKS document refresh attempts (HTTP fetches initiated)",
        unit="1",
    )
    m.create_observable_counter(
        "mdx_jwks_refresh_failures_total",
        callbacks=[_refresh_failures],
        description="JWKS refresh attempts that failed to fetch or parse",
        unit="1",
    )
    m.create_observable_counter(
        "mdx_jwks_rate_limited_refreshes_total",
        callbacks=[_rate_limited],
        description="JWKS refreshes suppressed by the storm-prevention rate limit",
        unit="1",
    )
