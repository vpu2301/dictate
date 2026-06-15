"""Sprint-08 Day-2 — Report status state machine.

Allowed transitions (from spec §2.2):

    draft       → finalized      via POST /reports/{id}/finalize
    draft       → cancelled      via POST /reports/{id}/cancel
    finalized   → draft          via POST /reports/{id}/revert-to-draft
                                 (author + within 1h)
    finalized   → signed         via sprint-09 signing (placeholder hook)
    finalized   → cancelled      via POST /reports/{id}/cancel
    signed      → amended        when an amendment is signed (sprint-09)

Everything else raises :class:`IllegalTransitionError` → HTTP 422.

Each transition is materialised as a single-statement
``UPDATE reports SET status=... WHERE id=$1 AND status=<expected>``.
The status-match WHERE clause is the optimistic check: if 0 rows
match, another transition raced — return 409 with the current state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final
from uuid import UUID

import asyncpg

from report_models import ReportStatus

logger = logging.getLogger(__name__)


REVERT_WINDOW: Final = timedelta(hours=1)


class TransitionAction(StrEnum):
    FINALIZE = "finalize"
    REVERT_TO_DRAFT = "revert_to_draft"
    SIGN = "sign"
    AMEND = "amend"
    CANCEL = "cancel"


# (from_status, action) -> to_status
_ALLOWED: Final[dict[tuple[ReportStatus, TransitionAction], ReportStatus]] = {
    (ReportStatus.DRAFT, TransitionAction.FINALIZE): ReportStatus.FINALIZED,
    (ReportStatus.DRAFT, TransitionAction.CANCEL): ReportStatus.CANCELLED,
    (ReportStatus.FINALIZED, TransitionAction.REVERT_TO_DRAFT): ReportStatus.DRAFT,
    (ReportStatus.FINALIZED, TransitionAction.SIGN): ReportStatus.SIGNED,
    (ReportStatus.FINALIZED, TransitionAction.CANCEL): ReportStatus.CANCELLED,
    (ReportStatus.SIGNED, TransitionAction.AMEND): ReportStatus.AMENDED,
}


class IllegalTransitionError(Exception):
    def __init__(self, from_status: ReportStatus, action: TransitionAction) -> None:
        self.from_status = from_status
        self.action = action
        super().__init__(f"action {action.value!r} not allowed from status {from_status.value!r}")


class ConcurrentTransitionError(Exception):
    """Another transaction transitioned the row before our UPDATE landed."""

    def __init__(self, observed_status: ReportStatus | None) -> None:
        self.observed_status = observed_status
        super().__init__(
            f"concurrent transition; report is now in status "
            f"{observed_status.value if observed_status else '<deleted>'}"
        )


class RevertWindowExceededError(Exception):
    pass


class NotPrimaryAuthorError(Exception):
    pass


class FinalizeValidationError(Exception):
    """Required sections / ICD-10 missing per template."""

    def __init__(self, problems: list[dict[str, str]]) -> None:
        self.problems = problems
        super().__init__(f"finalize validation failed: {len(problems)} problem(s)")


@dataclass(slots=True)
class TransitionResult:
    report_id: UUID
    from_status: ReportStatus
    to_status: ReportStatus
    action: TransitionAction


class ReportStateMachine:
    """Stateless verifier + applier of report status transitions.

    Single instance per service; methods take an asyncpg connection
    that has already been opened via ``tenant_connection``.
    """

    def expected_to(self, from_status: ReportStatus, action: TransitionAction) -> ReportStatus:
        try:
            return _ALLOWED[(from_status, action)]
        except KeyError as exc:
            raise IllegalTransitionError(from_status, action) from exc

    def allowed_actions(self, from_status: ReportStatus) -> list[TransitionAction]:
        return [act for (st, act) in _ALLOWED if st == from_status]

    # ── Atomic UPDATE helpers ───────────────────────────────────────

    async def _atomic_update_status(
        self,
        conn: asyncpg.Connection,
        *,
        report_id: UUID,
        expected_from: ReportStatus,
        to: ReportStatus,
        timestamp_col: str | None,
        extra_set: str = "",
        extra_args: tuple = (),
    ) -> None:
        sets = ["status = $2"]
        if timestamp_col is not None:
            sets.append(f"{timestamp_col} = now()")
        sets.append("updated_at = now()")
        if extra_set:
            sets.append(extra_set)
        args: list = [report_id, to.value, expected_from.value, *extra_args]
        sql = f"UPDATE reports SET {', '.join(sets)} WHERE id = $1 AND status = $3 RETURNING id"
        row = await conn.fetchrow(sql, *args)
        if row is None:
            current = await conn.fetchrow("SELECT status FROM reports WHERE id = $1", report_id)
            observed = ReportStatus(current["status"]) if current else None
            raise ConcurrentTransitionError(observed)

    # ── Public actions ──────────────────────────────────────────────

    async def finalize(
        self,
        conn: asyncpg.Connection,
        *,
        report_id: UUID,
    ) -> TransitionResult:
        to = self.expected_to(ReportStatus.DRAFT, TransitionAction.FINALIZE)
        await self._atomic_update_status(
            conn,
            report_id=report_id,
            expected_from=ReportStatus.DRAFT,
            to=to,
            timestamp_col="finalized_at",
        )
        return TransitionResult(report_id, ReportStatus.DRAFT, to, TransitionAction.FINALIZE)

    async def cancel(
        self,
        conn: asyncpg.Connection,
        *,
        report_id: UUID,
        from_status: ReportStatus,
        reason: str,
    ) -> TransitionResult:
        if from_status not in (ReportStatus.DRAFT, ReportStatus.FINALIZED):
            raise IllegalTransitionError(from_status, TransitionAction.CANCEL)
        to = ReportStatus.CANCELLED
        await self._atomic_update_status(
            conn,
            report_id=report_id,
            expected_from=from_status,
            to=to,
            timestamp_col="cancelled_at",
            extra_set="cancelled_reason = $4",
            extra_args=(reason,),
        )
        return TransitionResult(report_id, from_status, to, TransitionAction.CANCEL)

    async def revert_to_draft(
        self,
        conn: asyncpg.Connection,
        *,
        report_id: UUID,
        actor_user_id: UUID,
        now: datetime | None = None,
    ) -> TransitionResult:
        now = now or datetime.now(UTC)
        row = await conn.fetchrow(
            "SELECT status, primary_author_id, finalized_at FROM reports WHERE id = $1",
            report_id,
        )
        if row is None:
            raise ConcurrentTransitionError(None)
        current = ReportStatus(row["status"])
        if current != ReportStatus.FINALIZED:
            raise IllegalTransitionError(current, TransitionAction.REVERT_TO_DRAFT)
        if row["primary_author_id"] != actor_user_id:
            raise NotPrimaryAuthorError()
        finalized_at: datetime | None = row["finalized_at"]
        if finalized_at is None or (now - finalized_at) > REVERT_WINDOW:
            raise RevertWindowExceededError()

        to = ReportStatus.DRAFT
        await self._atomic_update_status(
            conn,
            report_id=report_id,
            expected_from=ReportStatus.FINALIZED,
            to=to,
            timestamp_col=None,
            extra_set="finalized_at = NULL",
        )
        return TransitionResult(
            report_id, ReportStatus.FINALIZED, to, TransitionAction.REVERT_TO_DRAFT
        )

    async def mark_signed(
        self,
        conn: asyncpg.Connection,
        *,
        report_id: UUID,
    ) -> TransitionResult:
        """Sprint-09 hook. Not called by sprint-08 paths."""
        to = self.expected_to(ReportStatus.FINALIZED, TransitionAction.SIGN)
        await self._atomic_update_status(
            conn,
            report_id=report_id,
            expected_from=ReportStatus.FINALIZED,
            to=to,
            timestamp_col="signed_at",
        )
        return TransitionResult(report_id, ReportStatus.FINALIZED, to, TransitionAction.SIGN)

    async def mark_amended(
        self,
        conn: asyncpg.Connection,
        *,
        report_id: UUID,
    ) -> TransitionResult:
        """Sprint-09 hook. Called when an amendment is signed."""
        to = self.expected_to(ReportStatus.SIGNED, TransitionAction.AMEND)
        await self._atomic_update_status(
            conn,
            report_id=report_id,
            expected_from=ReportStatus.SIGNED,
            to=to,
            timestamp_col=None,
        )
        return TransitionResult(report_id, ReportStatus.SIGNED, to, TransitionAction.AMEND)
