"""Service-wide singletons for report-service."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg
from opentelemetry import metrics
from redis.asyncio import Redis

from audit import AuditWriter, Severity
from auth import JwksCache
from crypto import Envelope, TenantKekRepository, build_master_key_provider
from db import create_pool
from storage import EncryptedObjectStore, S3Client

from . import audit_kinds
from .config import settings
from .domain.autosave_rate_limit import AutosaveRateLimiter
from .domain.cache import TemplateCache
from .domain.clip_rate_limit import ClipRateLimiter
from .domain.diff_cache import DiffCache
from .domain.draft_audit_buffer import DraftAuditBuffer
from .domain.search_audit_buffer import SearchAuditBuffer

logger = logging.getLogger(__name__)
_meter = metrics.get_meter("mdx.report")


@dataclass
class ServiceState:
    jwks_cache: JwksCache
    app_pool: asyncpg.Pool
    audit_writer_pool: asyncpg.Pool
    audit_writer: AuditWriter
    template_cache: TemplateCache
    # Sprint-08 additions.
    diff_cache: DiffCache
    autosave_rate_limiter: AutosaveRateLimiter
    draft_audit_buffer: DraftAuditBuffer
    # Sprint-12: the notification event bus. Publishing is fire-and-forget
    # (libs/notification_events.publish_event never raises), so a Redis
    # outage degrades notifications without touching report writes.
    redis: Redis
    # Sprint 15: audio replay (ADR-0037). Whole-object decrypt is the ONLY
    # read path — the GCM envelope has no range mode.
    crypto_pool: asyncpg.Pool
    audio_store: EncryptedObjectStore
    transcripts_store: EncryptedObjectStore
    clips_store: EncryptedObjectStore
    clip_rate_limiter: ClipRateLimiter
    # Sprint 15: aggregated search.expanded audit (ADR-0038).
    search_audit_buffer: SearchAuditBuffer
    # Metric handles (kept on state so routers don't recreate them).
    diff_cache_hit_metric: object
    autosave_conflicts_metric: object
    clips_created_metric: object
    clip_pipeline_latency_metric: object


async def build_state() -> ServiceState:
    jwks_cache = JwksCache(issuer_to_url={settings.auth_issuer: settings.auth_jwks_url})
    app_pool = await create_pool(
        settings.db_app_role_dsn,
        application_name=f"{settings.service_name}/app",
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )
    audit_writer_pool = await create_pool(
        settings.db_audit_writer_dsn,
        application_name=f"{settings.service_name}/audit_writer",
        min_size=1,
        max_size=4,
    )
    template_cache = TemplateCache(
        maxsize=settings.template_cache_maxsize,
        ttl_seconds=settings.template_cache_ttl_seconds,
    )
    audit_writer = AuditWriter(audit_writer_pool)
    diff_cache = DiffCache(max_entries=1024)
    autosave_rl = AutosaveRateLimiter(min_interval_s=5.0)

    diff_cache_hit_metric = _meter.create_counter(
        "mdx_reports_diff_cache_lookups_total",
        description="Diff endpoint cache lookups (label=hit)",
        unit="1",
    )
    autosave_conflicts_metric = _meter.create_counter(
        "mdx_reports_autosave_conflicts_total",
        description="409s returned by autosave path",
        unit="1",
    )
    clips_created_metric = _meter.create_counter(
        "mdx_audio_clips_created_total",
        description="Audio replay clips (labels: source_kind, outcome)",
        unit="1",
    )
    clip_pipeline_latency_metric = _meter.create_histogram(
        "mdx_audio_clip_pipeline_latency_ms_histogram",
        description="fetch+decrypt+slice+encode+store wall time in ms",
        unit="",
    )

    # Sprint 15: S3 + envelope crypto for audio replay (ADR-0037) — the
    # dictation-service main_deps wiring, ported. Same env names, same
    # dev master key.
    crypto_pool = await create_pool(
        settings.db_crypto_writer_dsn,
        application_name=f"{settings.service_name}/crypto_writer",
        min_size=1,
        max_size=2,
    )
    master = build_master_key_provider(
        provider=settings.master_key_provider,
        file_path=settings.master_key_path,
        vault_addr=settings.vault_addr,
        vault_token=settings.vault_token,
        vault_transit_key=settings.vault_transit_key,
        vault_transit_mount=settings.vault_transit_mount,
    )
    await master.startup_self_check()
    kek_repo = TenantKekRepository(pool=crypto_pool, master_key_provider=master)
    envelope = Envelope(master_key_provider=master, kek_repository=kek_repo)
    s3 = S3Client(
        endpoint_url=settings.s3_endpoint,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        region=settings.s3_region,
        use_ssl=settings.s3_use_ssl,
    )
    audio_store = EncryptedObjectStore(
        s3=s3, bucket=settings.s3_audio_bucket, envelope=envelope,
        disabled=settings.object_store_disabled,
    )
    transcripts_store = EncryptedObjectStore(
        s3=s3, bucket=settings.s3_transcripts_bucket, envelope=envelope,
        disabled=settings.object_store_disabled,
    )
    clips_store = EncryptedObjectStore(
        s3=s3, bucket=settings.s3_clips_bucket, envelope=envelope,
        disabled=settings.object_store_disabled,
    )

    async def _flush_draft_aggregate(tenant_id, report_id, session_id, entry) -> None:
        await audit_writer.write_event(
            tenant_id=tenant_id,
            kind=audit_kinds.REPORT_DRAFT_UPDATED,
            actor_sub=entry.actor_user_id,
            actor_role=None,
            target_kind="report",
            target_id=report_id,
            payload={
                "dictation_session_id": str(session_id) if session_id else None,
                "autosave_count": entry.autosave_count,
                "start_at": entry.start_at.isoformat(),
                "end_at": entry.end_at.isoformat(),
                "final_version_number": entry.final_version_number,
            },
            severity=Severity.INFO,
        )

    draft_audit_buffer = DraftAuditBuffer(flush_fn=_flush_draft_aggregate)
    draft_audit_buffer.start()

    async def _flush_search_aggregate(tenant_id, count, expanded_terms_total) -> None:
        await audit_writer.write_event(
            tenant_id=tenant_id,
            kind=audit_kinds.SEARCH_EXPANDED,
            actor_sub=None,
            actor_role="system",
            target_kind="report",
            target_id=None,
            payload={"count": count, "expanded_terms_total": expanded_terms_total},
            severity=Severity.INFO,
        )

    search_audit_buffer = SearchAuditBuffer(flush_fn=_flush_search_aggregate)
    search_audit_buffer.start()

    redis = Redis.from_url(settings.redis_url, decode_responses=False)

    return ServiceState(
        jwks_cache=jwks_cache,
        app_pool=app_pool,
        audit_writer_pool=audit_writer_pool,
        audit_writer=audit_writer,
        template_cache=template_cache,
        diff_cache=diff_cache,
        autosave_rate_limiter=autosave_rl,
        draft_audit_buffer=draft_audit_buffer,
        redis=redis,
        crypto_pool=crypto_pool,
        audio_store=audio_store,
        transcripts_store=transcripts_store,
        clips_store=clips_store,
        clip_rate_limiter=ClipRateLimiter(
            redis, per_hour=settings.clips_per_user_per_hour
        ),
        search_audit_buffer=search_audit_buffer,
        diff_cache_hit_metric=diff_cache_hit_metric,
        autosave_conflicts_metric=autosave_conflicts_metric,
        clips_created_metric=clips_created_metric,
        clip_pipeline_latency_metric=clip_pipeline_latency_metric,
    )


async def teardown_state(state: ServiceState) -> None:
    await state.draft_audit_buffer.stop()
    await state.search_audit_buffer.stop()
    await state.redis.aclose()
    await state.jwks_cache.aclose()
    await state.app_pool.close()
    await state.audit_writer_pool.close()
    await state.crypto_pool.close()
