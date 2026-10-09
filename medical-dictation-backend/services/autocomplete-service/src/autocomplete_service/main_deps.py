"""Service-wide singletons."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg
from opentelemetry import metrics

from audit import AuditWriter
from auth import JwksCache
from db import create_pool

from .config import settings
from .integrations.asr_client import AsrClient, AsrClientConfig
from .rate_limit import PhraseWriteRateLimiter
from .telemetry_buffer import TelemetryBuffer
from .trie.cache import TrieCache

logger = logging.getLogger(__name__)
_meter = metrics.get_meter("mdx.autocomplete")


@dataclass
class ServiceState:
    jwks_cache: JwksCache
    app_pool: asyncpg.Pool
    audit_writer_pool: asyncpg.Pool
    audit_writer: AuditWriter
    redis: object  # redis.asyncio.Redis — held for the readiness probe
    phrase_rate_limiter: object  # PhraseWriteRateLimiter
    pii_rejections_metric: object
    trie_cache: TrieCache
    telemetry_buffer: TelemetryBuffer
    suggest_cache_metric: object
    suggest_latency_metric: object
    telemetry_event_metric: object
    telemetry_redaction_metric: object
    # Eval scoring's transcription path (0091). Built unconditionally — it
    # opens no connection until a run is pumped, so a deployment that never
    # scores pays nothing for it.
    asr_client: AsrClient


async def build_state() -> ServiceState:
    jwks_cache = JwksCache(issuer_to_url={settings.auth_issuer: settings.auth_jwks_url})
    app_pool = await create_pool(
        settings.db_app_role_dsn,
        application_name=f"{settings.service_name}/app",
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        # libs/db.create_pool always sets statement_cache_size=0 (transaction-
        # pooler safe), so we don't pass it explicitly — it isn't a kwarg.
    )
    audit_writer_pool = await create_pool(
        settings.db_audit_writer_dsn,
        application_name=f"{settings.service_name}/audit_writer",
        min_size=1,
        max_size=4,
    )
    audit_writer = AuditWriter(audit_writer_pool)

    from redis.asyncio import Redis

    redis = Redis.from_url(settings.redis_url, decode_responses=False)
    # Cache health instruments (step-03 §4.3): the degraded counter feeds
    # the Redis-down / lock-storm visibility; the size histogram feeds the
    # 50k-phrase marisa-trie upgrade watch (ADR-0025).
    degraded_metric = _meter.create_counter(
        "mdx_autocomplete_degraded_total",
        description="Suggest requests served by the degraded direct-DB path (label=reason)",
        unit="1",
    )
    trie_build_seconds = _meter.create_histogram(
        "mdx_autocomplete_trie_build_seconds",
        description="Trie build duration (DB fetch + build)",
        unit="s",
    )
    trie_size_bytes = _meter.create_histogram(
        "mdx_autocomplete_trie_size_bytes",
        description="Serialized trie blob size — feeds the 50k marisa upgrade watch",
        unit="By",
    )
    trie_cache = TrieCache(
        redis,
        ttl_seconds=settings.trie_cache_ttl_seconds,
        degraded_metric=degraded_metric,
        build_seconds_metric=trie_build_seconds,
        size_bytes_metric=trie_size_bytes,
    )

    telemetry_dropped_metric = _meter.create_counter(
        "mdx_autocomplete_telemetry_dropped_total",
        description="Telemetry rows shed (label reason=buffer_overflow|flush_failed)",
        unit="1",
    )
    pii_rejections_metric = _meter.create_counter(
        "mdx_autocomplete_phrase_write_pii_rejections_total",
        description="Corpus writes rejected by the PII scrubber (label=pattern)",
        unit="1",
    )
    telemetry_buffer = TelemetryBuffer(
        app_pool,
        flush_interval_s=settings.telemetry_flush_interval_s,
        flush_batch=settings.telemetry_flush_batch,
        dropped_metric=telemetry_dropped_metric,
    )
    telemetry_buffer.start()

    # Roll-up freshness gauge — the RollupStale alert fires on its age.
    # unit "" so the exporter does not suffix the contract name.
    from opentelemetry.metrics import CallbackOptions, Observation

    from .jobs import rollup as rollup_job

    def _rollup_ts_callback(_options: CallbackOptions) -> list[Observation]:
        return [Observation(rollup_job.last_success_unix())]

    _meter.create_observable_gauge(
        "mdx_autocomplete_rollup_last_run_unix_ts",
        callbacks=[_rollup_ts_callback],
        description="Unix timestamp of the last successful roll-up in this process",
        unit="",
    )

    def _corpus_size_callback(_options: CallbackOptions) -> list[Observation]:
        return [
            Observation(n, {"source": source})
            for source, n in rollup_job.corpus_size_by_source().items()
        ]

    # Sprint 15: Layer C acceptance rate (ADR-0036) — the ghost-text quality
    # metric and kill-switch input. Global; refreshed by the nightly roll-up.
    def _layer_c_acceptance_callback(_options: CallbackOptions) -> list[Observation]:
        return [Observation(rollup_job.layer_c_acceptance_rate())]

    _meter.create_observable_gauge(
        "mdx_layer_c_acceptance_rate",
        callbacks=[_layer_c_acceptance_callback],
        description="Layer C completions accepted / impressions (last rolled-up day)",
        unit="",
    )

    def _layer_c_events_callback(_options: CallbackOptions) -> list[Observation]:
        return [
            Observation(n, {"event": event})
            for event, n in rollup_job.layer_c_events_by_type().items()
        ]

    _meter.create_observable_gauge(
        "mdx_layer_c_telemetry_events",
        callbacks=[_layer_c_events_callback],
        description="Layer C telemetry rows by event type (last rolled-up day)",
        unit="",
    )

    _meter.create_observable_gauge(
        "mdx_autocomplete_corpus_size",
        callbacks=[_corpus_size_callback],
        description=(
            "Enabled corpus rows by source, refreshed at roll-up. RLS limits "
            "the unscoped counter to system rows; scoped counts are a "
            "sprint-16 admin-surface concern."
        ),
        unit="",
    )

    suggest_cache_metric = _meter.create_counter(
        "mdx_autocomplete_cache_lookups_total",
        description="Suggest cache lookups (label=hit)",
        unit="1",
    )
    # Name matches the as-built Grafana/k6 contract
    # (…_suggest_latency_ms_histogram_bucket after the exporter's suffixes).
    # unit deliberately empty: the collector's prometheus exporter appends
    # the unit name as a suffix, which would break the dashboard-contract
    # metric name (…_suggest_latency_ms_histogram_bucket). Values are ms.
    suggest_latency_metric = _meter.create_histogram(
        "mdx_autocomplete_suggest_latency_ms_histogram",
        description="End-to-end suggest latency in ms (label path=hit|miss|degraded|snippet)",
        unit="",
    )
    telemetry_event_metric = _meter.create_counter(
        "mdx_autocomplete_telemetry_events_total",
        description="Telemetry events (label=event)",
        unit="1",
    )
    telemetry_redaction_metric = _meter.create_counter(
        "mdx_autocomplete_telemetry_scrubber_redactions_total",
        description="PII redactions in telemetry prefixes",
        unit="1",
    )

    return ServiceState(
        jwks_cache=jwks_cache,
        app_pool=app_pool,
        audit_writer_pool=audit_writer_pool,
        audit_writer=audit_writer,
        redis=redis,
        phrase_rate_limiter=PhraseWriteRateLimiter(
            redis, per_hour=settings.phrase_max_creates_per_hour
        ),
        pii_rejections_metric=pii_rejections_metric,
        trie_cache=trie_cache,
        telemetry_buffer=telemetry_buffer,
        suggest_cache_metric=suggest_cache_metric,
        suggest_latency_metric=suggest_latency_metric,
        telemetry_event_metric=telemetry_event_metric,
        telemetry_redaction_metric=telemetry_redaction_metric,
        asr_client=AsrClient(
            config=AsrClientConfig(
                base_url=settings.asr_service_base_url,
                timeout_seconds=settings.asr_request_timeout_seconds,
            )
        ),
    )


async def teardown_state(state: ServiceState) -> None:
    await state.telemetry_buffer.stop()
    await state.asr_client.aclose()
    await state.jwks_cache.aclose()
    await state.redis.aclose()  # type: ignore[attr-defined]
    await state.app_pool.close()
    await state.audit_writer_pool.close()
