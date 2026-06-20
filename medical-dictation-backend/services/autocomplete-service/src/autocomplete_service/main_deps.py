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
    trie_cache: TrieCache
    telemetry_buffer: TelemetryBuffer
    suggest_cache_metric: object
    telemetry_event_metric: object
    telemetry_redaction_metric: object


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
    trie_cache = TrieCache(redis, ttl_seconds=settings.trie_cache_ttl_seconds)

    telemetry_buffer = TelemetryBuffer(
        app_pool,
        flush_interval_s=settings.telemetry_flush_interval_s,
        flush_batch=settings.telemetry_flush_batch,
    )
    telemetry_buffer.start()

    suggest_cache_metric = _meter.create_counter(
        "mdx_autocomplete_cache_lookups_total",
        description="Suggest cache lookups (label=hit)",
        unit="1",
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
        trie_cache=trie_cache,
        telemetry_buffer=telemetry_buffer,
        suggest_cache_metric=suggest_cache_metric,
        telemetry_event_metric=telemetry_event_metric,
        telemetry_redaction_metric=telemetry_redaction_metric,
    )


async def teardown_state(state: ServiceState) -> None:
    await state.telemetry_buffer.stop()
    await state.jwks_cache.aclose()
    await state.app_pool.close()
    await state.audit_writer_pool.close()
