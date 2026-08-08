"""Immutable corpus snapshots (spec D7) + license admission (licenses live
in constants; the register is docs/corpus/licenses.md)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

import asyncpg
from audit import AuditWriter, Severity
from db import tenant_connection

from evidence_ingest import audit_kinds
from evidence_ingest.constants import ALLOWED_SNAPSHOT_LICENSES
from evidence_ingest.domain import repository

logger = logging.getLogger(__name__)


class SnapshotDirtyError(Exception):
    def __init__(self, pending: int) -> None:
        super().__init__(f"{pending} ingest job(s) still pending")
        self.pending = pending


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    snapshot_id: UUID
    member_count: int
    license_excluded: int


async def build_snapshot(
    *,
    pool: asyncpg.Pool,
    audit_writer: AuditWriter,
    tenant_id: UUID,
    label: str,
) -> SnapshotResult:
    async with tenant_connection(pool, tenant_id) as conn:
        pending = await repository.pending_job_count(conn, tenant_id=tenant_id)
        if pending:
            raise SnapshotDirtyError(pending)
        members = await repository.snapshot_members(
            conn, tenant_id=tenant_id, allowed_licenses=ALLOWED_SNAPSHOT_LICENSES
        )
        excluded = await repository.excluded_license_count(
            conn, tenant_id=tenant_id, allowed_licenses=ALLOWED_SNAPSHOT_LICENSES
        )
        snapshot_id = await repository.create_snapshot(
            conn,
            tenant_id=tenant_id,
            label=label,
            member_versions=[m["version_id"] for m in members],
        )
    if excluded:
        # Alert path (spec §7): visible in logs/metrics; documents stay
        # indexed for review but never ship in a snapshot.
        logger.warning(
            "snapshot.license_excluded",
            extra={"tenant_id": str(tenant_id), "label": label, "excluded": excluded},
        )
    await audit_writer.write_event(
        tenant_id=tenant_id,
        kind=audit_kinds.SNAPSHOT_CREATED,
        target_kind="corpus_snapshots",
        target_id=snapshot_id,
        payload={"label": label, "members": len(members), "license_excluded": excluded},
        severity=Severity.INFO,
    )
    return SnapshotResult(
        snapshot_id=snapshot_id, member_count=len(members), license_excluded=excluded
    )
