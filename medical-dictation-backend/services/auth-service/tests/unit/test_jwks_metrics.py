"""Unit tests for JWKS → OTel observable-counter bridge.

Drives a real ``JwksCache`` over a mocked JWKS endpoint and asserts the OTel
series collected from an in-memory reader track the cache's in-memory
counters. Guards the ``JwksCacheHitRatioLow`` alert's series against silently
disappearing again.
"""

from __future__ import annotations

import httpx
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from auth import JwksCache
from auth_service.jwks_metrics import instrument_jwks_cache

_JWKS = {"keys": [{"kid": "k1", "kty": "RSA", "n": "x", "e": "AQAB"}]}


def _collect(reader: InMemoryMetricReader) -> dict[str, float]:
    """Flatten the latest collection into ``{metric_name: value}``."""
    data = reader.get_metrics_data()
    out: dict[str, float] = {}
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                for point in metric.data.data_points:
                    out[metric.name] = point.value
    return out


def _cache_with_reader(handler: httpx.MockTransport) -> tuple[JwksCache, InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    cache = JwksCache(
        issuer_to_url={"iss": "https://idp/jwks"},
        http_client=httpx.AsyncClient(transport=handler),
    )
    instrument_jwks_cache(cache, meter=provider.get_meter("test"))
    return cache, reader


async def test_hit_and_miss_counters_surface_to_otel() -> None:
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, json=_JWKS))
    cache, reader = _cache_with_reader(transport)

    # First lookup → miss + fetch; second identical lookup → hit.
    await cache.get_key("iss", "k1")
    await cache.get_key("iss", "k1")

    series = _collect(reader)
    assert series["mdx_jwks_cache_misses_total"] == 1
    assert series["mdx_jwks_cache_hits_total"] == 1
    assert series["mdx_jwks_refresh_attempts_total"] == 1
    assert series["mdx_jwks_refresh_failures_total"] == 0

    await cache.aclose()


async def test_fetch_failure_increments_failure_series() -> None:
    transport = httpx.MockTransport(lambda _req: httpx.Response(503))
    cache, reader = _cache_with_reader(transport)

    from auth.exceptions import JwksFetchError

    with pytest.raises(JwksFetchError):
        await cache.get_key("iss", "k1")

    series = _collect(reader)
    assert series["mdx_jwks_refresh_attempts_total"] == 1
    assert series["mdx_jwks_refresh_failures_total"] == 1
    assert series["mdx_jwks_cache_hits_total"] == 0

    await cache.aclose()
