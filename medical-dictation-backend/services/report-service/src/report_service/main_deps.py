"""Service-wide singletons for report-service."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg
from opentelemetry import metrics

from audit import AuditWriter, Severity
from auth import JwksCache
from db import create_pool

from .config import settings
from .domain.autosave_rate_limit import AutosaveRateLimiter
from .domain.cache import TemplateCache
from .domain.diff_cache import DiffCache
from .domain.draft_audit_buffer import DraftAuditBuffer
from . import audit_kinds

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
    # Metric handles (kept on state so routers don't recreate them).
    diff_cache_hit_metric: object
    autosave_conflicts_metric: object


async def build_state() -> ServiceState:
    jwks_cache = JwksCache(
        issuer_to_url={settings.auth_issuer: settings.auth_jwks_url}
    )
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

    return ServiceState(
        jwks_cache=jwks_cache,
        app_pool=app_pool,
        audit_writer_pool=audit_writer_pool,
        audit_writer=audit_writer,
        template_cache=template_cache,
        diff_cache=diff_cache,
        autosave_rate_limiter=autosave_rl,
        draft_audit_buffer=draft_audit_buffer,
        diff_cache_hit_metric=diff_cache_hit_metric,
        autosave_conflicts_metric=autosave_conflicts_metric,
    )


async def teardown_state(state: ServiceState) -> None:
    await state.draft_audit_buffer.stop()
    await state.jwks_cache.aclose()
    await state.app_pool.close()
    await state.audit_writer_pool.close()
