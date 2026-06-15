"""GET /audit/events (paginated read) + GET /audit/verify (chain verifier).

Both routes are tenant-scoped via ``Claims.tid``. Role gate: ``auditor``
*or* ``tenant_admin`` — anyone else gets 403. Day 7 replaces the inline
role check in ``deps.require_audit_role`` with the formal
``requires(action, target_kind)`` permission matrix.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import Claims

from ..deps import get_state, requires

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/events", summary="List audit events for the caller's tenant")
async def list_events(
    claims: Annotated[Claims, Depends(requires("audit.read", "audit"))],
    from_seq: Annotated[int | None, Query(ge=1, description="Min seq (inclusive)")] = None,
    to_seq: Annotated[int | None, Query(ge=1, description="Max seq (inclusive)")] = None,
    kind: Annotated[str | None, Query(description="Exact kind match")] = None,
    actor_sub: Annotated[UUID | None, Query(description="Filter by actor UUID")] = None,
    since: Annotated[datetime | None, Query(description="created_at >= since")] = None,
    until: Annotated[datetime | None, Query(description="created_at <= until")] = None,
    severity: Annotated[str | None, Query(description="info | warn | sec | error")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: Annotated[int | None, Query(ge=1, description="Resume from seq > cursor")] = None,
) -> dict[str, Any]:
    """Paginated, filtered read of audit.events for the caller's tenant.

    Pagination cursor is the last seq seen. Pass it as ``cursor`` on the
    next call to fetch events with ``seq > cursor``. Combined with
    ``limit`` this gives stable forward-only paging without skipped rows.
    """
    state = get_state()
    tenant_id = claims.tid

    conditions: list[str] = ["tenant_id = $1"]
    params: list[Any] = [tenant_id]

    def add(cond: str, value: Any) -> None:
        params.append(value)
        conditions.append(cond.replace("?", f"${len(params)}"))

    if from_seq is not None:
        add("seq >= ?", from_seq)
    if to_seq is not None:
        add("seq <= ?", to_seq)
    if kind is not None:
        add("kind = ?", kind)
    if actor_sub is not None:
        add("actor_sub = ?", actor_sub)
    if since is not None:
        add("created_at >= ?", since)
    if until is not None:
        add("created_at <= ?", until)
    if severity is not None:
        if severity not in ("info", "warn", "sec", "error"):
            raise HTTPException(status_code=422, detail=f"invalid severity {severity!r}")
        add("severity = ?", severity)
    if cursor is not None:
        add("seq > ?", cursor)

    sql = (
        "SELECT seq, created_at, actor_sub, actor_role, kind, target_kind, target_id, "
        "       payload_jcs::text AS payload_jcs, severity "
        f"FROM audit.events WHERE {' AND '.join(conditions)} "
        f"ORDER BY seq LIMIT {limit}"
    )

    async with state.audit_reader_pool.acquire() as conn, conn.transaction(readonly=True):
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        rows = await conn.fetch(sql, *params)

    events = [
        {
            "seq": int(r["seq"]),
            "created_at": r["created_at"].isoformat(),
            "actor_sub": str(r["actor_sub"]) if r["actor_sub"] is not None else None,
            "actor_role": r["actor_role"],
            "kind": r["kind"],
            "target_kind": r["target_kind"],
            "target_id": r["target_id"],
            "payload": json.loads(r["payload_jcs"]),
            "severity": r["severity"],
        }
        for r in rows
    ]
    next_cursor = events[-1]["seq"] if len(events) == limit else None
    return {"events": events, "next_cursor": next_cursor, "count": len(events)}


@router.get("/verify", summary="Verify the audit chain for the caller's tenant")
async def verify_chain(
    claims: Annotated[Claims, Depends(requires("audit.verify", "audit"))],
    from_seq: Annotated[int, Query(ge=1)] = 1,
    to_seq: Annotated[int | None, Query(ge=1)] = None,
) -> dict[str, Any]:
    """Walk the chain, recompute every hash, and report the first divergence
    (if any). Returns ``ok``, ``events_checked``, ``last_seq``/``last_hash``,
    and on failure ``first_divergence_seq`` + ``divergence_reason`` +
    ``expected_hash``/``actual_hash``.
    """
    state = get_state()
    report = await state.audit_verifier.verify_chain(claims.tid, from_seq=from_seq, to_seq=to_seq)
    return {
        "ok": report.ok,
        "tenant_id": str(report.tenant_id),
        "from_seq": report.from_seq,
        "to_seq": report.to_seq,
        "events_checked": report.events_checked,
        "last_seq": report.last_seq,
        "last_hash": report.last_hash.hex() if report.last_hash else None,
        "first_divergence_seq": report.first_divergence_seq,
        "divergence_reason": (report.divergence_reason.value if report.divergence_reason else None),
        "expected_hash": report.expected_hash.hex() if report.expected_hash else None,
        "actual_hash": report.actual_hash.hex() if report.actual_hash else None,
    }
