"""The erasure engine (S11 step 07) — provable destruction, honest retention.

``execute_erasure`` runs strict phases:

1. **Lock + preflight** (app_role, read-only): a session advisory lock on
   the request id (cron + manual can never double-run); request must be
   ``approved`` with grace elapsed (→ ``executing``, audited) or already
   ``executing`` (a crashed run — the idempotent re-run IS the recovery).
2. **Inventory snapshot** — ``enumerate_patient`` over the same fan-out
   map the DSAR export renders.
3. **Destruction** — every eraser from ``ERASERS_IN_ORDER``, each in its
   own transaction under the ``mdx_erasure`` role (the role split is the
   last-line defense: a classification bug cannot delete what the role
   can't touch). Object-before-row for blob artifacts (crypto-shred);
   every destroyed item audited. Retention is decided inside the erasers
   (signed reports inside REPORT_RETENTION_YEARS, ALL consents, ALL
   privacy requests, retained-resource envelopes) and reported, never
   silently skipped.
4. **Identity overwrite** — the tombstone write (last eraser in order).
5. **Completion** — ``report_of_execution`` (destroyed[] + retained[] with
   machine-stable bases), request → ``completed``, ``erasure.executed``
   (sec) with counts — ids only, never identity strings.

Failure: the request STAYS ``executing`` with ``last_error`` recorded —
erasure has no failed state by design; partial destruction mid-run is
safe because every eraser tolerates already-gone and the re-run
completes the inventory.

Audit-channel note (deviation from the step sketch, recorded): audit
events cannot be written on the mdx_erasure connection — only the
``audit_writer`` role may INSERT into ``audit.events`` (rule 5). Each
artifact's event is therefore emitted immediately AFTER its delete
transaction commits; a crash between commit and emit can at worst
duplicate an artifact event on re-run (ids only), while
``erasure.executed`` is emitted exactly once, after the completion
transition succeeds.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg

from audit import AuditWriter, Severity
from db import create_pool, tenant_connection
from storage import EncryptedObjectStore, S3Client

from .. import audit_kinds
from ..config import settings
from ..domain import privacy_repository
from .erasers import ERASERS_IN_ORDER, DestroyedItem, EraserContext, RetainedItem
from .fanout import enumerate_patient

logger = logging.getLogger(__name__)

# S11 step 08 — observability. Labels: enums only, never ids. Recorded in
# whatever MeterProvider the process configured (the scheduler pushes via
# OTLP + flush; the manual CLI runs with the no-op provider unless the
# operator exports OTEL_* — metrics are advisory, audit is the record).
from opentelemetry import metrics as _otel_metrics  # noqa: E402

_meter = _otel_metrics.get_meter("core-service.erasure")
_execution_seconds = _meter.create_histogram(
    "mdx_erasure_execution_seconds", unit="s", description="Erasure execution duration"
)
_artifacts_destroyed = _meter.create_counter(
    "mdx_erasure_artifacts_destroyed_total", description="Destroyed artifacts by kind"
)

ENGINE_VERSION = "erasure-engine/1"


class ErasureRefusedError(RuntimeError):
    """Preflight refused (wrong state, grace running, already locked…)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass
class ErasureRuntime:
    """Pools + stores the engine needs. Built once per process."""

    app_pool: asyncpg.Pool
    erasure_pool: asyncpg.Pool
    audit_writer: AuditWriter
    ctx: EraserContext
    s3: S3Client

    @classmethod
    async def build(cls) -> ErasureRuntime:
        from crypto import Envelope, TenantKekRepository, build_master_key_provider

        app_pool = await create_pool(
            settings.db_app_role_dsn,
            application_name=f"{settings.service_name}/erasure-app",
            min_size=1, max_size=2,
        )
        erasure_pool = await create_pool(
            settings.db_erasure_dsn,
            application_name=f"{settings.service_name}/erasure",
            min_size=1, max_size=2,
        )
        audit_pool = await create_pool(
            settings.db_audit_writer_dsn,
            application_name=f"{settings.service_name}/erasure-audit",
            min_size=1, max_size=2,
        )
        crypto_pool = await create_pool(
            settings.db_crypto_writer_dsn,
            application_name=f"{settings.service_name}/erasure-crypto",
            min_size=1, max_size=2,
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
        envelope = Envelope(
            master_key_provider=master,
            kek_repository=TenantKekRepository(pool=crypto_pool, master_key_provider=master),
        )
        s3 = S3Client(
            endpoint_url=settings.s3_endpoint,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            region=settings.s3_region,
            use_ssl=settings.s3_use_ssl,
        )

        async def _delete_pdf_by_uri(uri: str) -> None:
            without_scheme = uri.split("://", 1)[1]
            bucket, key = without_scheme.split("/", 1)
            await s3.delete_object(bucket=bucket, key=key)

        ctx = EraserContext(
            audio_store=EncryptedObjectStore(
                s3=s3, bucket=settings.s3_audio_bucket, envelope=envelope
            ),
            transcript_store=EncryptedObjectStore(
                s3=s3, bucket=settings.s3_transcripts_bucket, envelope=envelope
            ),
            document_store=EncryptedObjectStore(
                s3=s3, bucket=settings.s3_patient_docs_bucket, envelope=envelope
            ),
            report_retention_years=settings.report_retention_years,
            pdf_object_deleter=_delete_pdf_by_uri,
        )
        return cls(
            app_pool=app_pool,
            erasure_pool=erasure_pool,
            audit_writer=AuditWriter(audit_pool),
            ctx=ctx,
            s3=s3,
        )


async def execute_erasure(
    runtime: ErasureRuntime,
    *,
    tenant_id: UUID,
    request_id: UUID,
    operator: str = "scheduler",
) -> dict[str, Any]:
    """Execute one approved erasure request. Returns report_of_execution.

    Raises :class:`ErasureRefusedError` when preflight declines (not approved,
    grace running, another run holds the lock, patient already erased on a
    fresh request).
    """
    # Session advisory lock held on a dedicated app connection for the
    # whole run — cron + manual entry can never double-execute.
    async with runtime.app_pool.acquire() as lock_conn:
        got_lock = await lock_conn.fetchval(
            "SELECT pg_try_advisory_lock(hashtextextended($1::text, 0))",
            str(request_id),
        )
        if not got_lock:
            raise ErasureRefusedError("already_running", "another execution holds the lock")
        try:
            return await _execute_locked(
                runtime, tenant_id=tenant_id, request_id=request_id, operator=operator
            )
        finally:
            await lock_conn.fetchval(
                "SELECT pg_advisory_unlock(hashtextextended($1::text, 0))",
                str(request_id),
            )


async def _execute_locked(
    runtime: ErasureRuntime, *, tenant_id: UUID, request_id: UUID, operator: str
) -> dict[str, Any]:
    import time as _time

    _exec_started = _time.monotonic()
    # ── Phase 1: preflight ────────────────────────────────────────────
    async with tenant_connection(runtime.app_pool, tenant_id) as conn:
        row = await privacy_repository.get_request(conn, request_id=request_id)
        if row is None or row["kind"] != "erasure":
            raise ErasureRefusedError("not_found", "no such erasure request in this tenant")
        patient_id: UUID = row["patient_id"]
        patient_status = await conn.fetchval(
            "SELECT status FROM patients WHERE id = $1", patient_id
        )

        if row["status"] == "approved":
            if patient_status == "erased":
                raise ErasureRefusedError(
                    "patient_already_erased",
                    "a fresh request against an erased patient must be rejected, not run",
                )
            started = await privacy_repository.mark_executing(conn, request_id=request_id)
            if started is None:
                raise ErasureRefusedError(
                    "grace_period_active",
                    f"scheduled_for={row['scheduled_for']} has not elapsed",
                )
        elif row["status"] == "executing":
            logger.info("erasure.rerun request=%s (recovery)", request_id)
        else:
            raise ErasureRefusedError("invalid_state", f"status={row['status']!r}")

        inventory = await enumerate_patient(conn, patient_id)

    await runtime.audit_writer.write_event(
        tenant_id=tenant_id,
        kind=audit_kinds.ERASURE_EXECUTING,
        actor_sub=None,
        actor_role="erasure-engine",
        target_kind="patient",
        target_id=str(patient_id),
        payload={
            "request_id": str(request_id),
            "operator": operator,
            "inventory_counts": inventory.counts,
        },
        severity=Severity.SEC,
    )

    # ── Phases 2–4: destruction along the map, under mdx_erasure ─────
    destroyed: list[DestroyedItem] = []
    retained: list[RetainedItem] = []
    try:
        for eraser in ERASERS_IN_ORDER:
            async with tenant_connection(runtime.erasure_pool, tenant_id) as conn:
                d, r = await eraser(conn, patient_id, runtime.ctx)
            destroyed += d
            retained += r
            for item in d:
                await runtime.audit_writer.write_event(
                    tenant_id=tenant_id,
                    kind=(
                        audit_kinds.ASR_AUDIO_DELETED
                        if item.kind == "recording"
                        else audit_kinds.ERASURE_ARTIFACT_DESTROYED
                    ),
                    actor_sub=None,
                    actor_role="erasure-engine",
                    target_kind=item.kind,
                    target_id=str(item.id),
                    payload={"request_id": str(request_id), "detail": item.detail},
                    severity=Severity.SEC,
                )
    except Exception as exc:
        # No 'failed' state by design — record and stay 'executing'; the
        # idempotent re-run is the recovery (every eraser tolerates gone).
        logger.exception("erasure.execution_error request=%s", request_id)
        async with tenant_connection(runtime.app_pool, tenant_id) as conn:
            await conn.execute(
                "UPDATE patient_privacy_requests SET last_error = $2 WHERE id = $1",
                request_id,
                f"{type(exc).__name__}: {exc}"[:500],
            )
        raise

    # ── Phase 5: completion ───────────────────────────────────────────
    _executed_at = datetime.now(UTC)
    report = {
        "executed_at": _executed_at.isoformat(),
        "engine_version": ENGINE_VERSION,
        "operator": operator,
        # Backups-vs-erasure horizon (ADR-0028, runbooks/erasure.md):
        # erased data persists in encrypted backups until the retention
        # rotation completes — this is the "fully purged from backups
        # by" date the DPO reports to the data subject.
        "backups_purged_by": (
            _executed_at + timedelta(days=settings.backup_retention_days)
        ).date().isoformat(),
        "destroyed": [
            {"kind": i.kind, "id": str(i.id), "detail": i.detail} for i in destroyed
        ],
        "retained": [
            {"kind": i.kind, "id": str(i.id), "legal_basis": i.legal_basis}
            for i in retained
        ],
        "counts": {
            "destroyed": len(destroyed),
            "retained": len(retained),
            "inventory_before": inventory.counts,
        },
    }
    _execution_seconds.record(_time.monotonic() - _exec_started)
    async with tenant_connection(runtime.app_pool, tenant_id) as conn:
        completed = await privacy_repository.mark_completed(
            conn, request_id=request_id, report_of_execution=report
        )
        assert completed is not None
        await conn.execute(
            "UPDATE patient_privacy_requests SET last_error = NULL WHERE id = $1",
            request_id,
        )

    await runtime.audit_writer.write_event(
        tenant_id=tenant_id,
        kind=audit_kinds.ERASURE_EXECUTED,
        actor_sub=None,
        actor_role="erasure-engine",
        target_kind="patient",
        target_id=str(patient_id),
        payload={
            "request_id": str(request_id),
            "destroyed": len(destroyed),
            "retained": len(retained),
            "engine_version": ENGINE_VERSION,
        },
        severity=Severity.SEC,
    )
    for item in destroyed:
        _artifacts_destroyed.add(1, attributes={"kind": item.kind})
    logger.info(
        "erasure.completed request=%s destroyed=%d retained=%d",
        request_id, len(destroyed), len(retained),
    )
    return report
