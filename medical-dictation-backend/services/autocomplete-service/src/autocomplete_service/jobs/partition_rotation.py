"""Monthly partition rotation for autocomplete_telemetry.

Runs in-process (service lifespan, MDX_BACKGROUND_JOBS) daily and at
startup: ensures the CURRENT + NEXT TWO months' partitions exist, then
enforces the 90-day retention by DETACH+DROP of partitions whose range
ended more than 90 days ago (via the SECURITY DEFINER function from
migration 0040 — app_role owns no DDL). Every dropped partition is
logged by name.

Cold-storage archival BEFORE drop is named sprint-16 scope — until then
retention is destructive and the drop log is the only record.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime

import asyncpg

from db import create_pool

from .. import repository as repo
from ..config import settings

logger = logging.getLogger(__name__)

MONTHS_AHEAD = 2  # ensure current + next two months
RETENTION_DAYS = 90


def _month_start(year: int, month: int) -> datetime:
    while month > 12:
        year, month = year + 1, month - 12
    return datetime(year, month, 1, tzinfo=UTC)


def _partition_bounds(now: datetime) -> list[tuple[datetime, datetime]]:
    """Month boundaries for the CURRENT and next ``MONTHS_AHEAD`` months.

    Covering the current month (not just future ones) lets a service that
    was down over a month boundary self-heal on startup instead of
    dropping every telemetry row until a human intervenes.
    """
    return [
        (
            _month_start(now.year, now.month + offset),
            _month_start(now.year, now.month + offset + 1),
        )
        for offset in range(MONTHS_AHEAD + 1)
    ]


async def ensure_partitions(app_pool: asyncpg.Pool) -> list[str]:
    now = datetime.now(UTC)
    names: list[str] = []
    async with app_pool.acquire() as conn:
        for start, end in _partition_bounds(now):
            names.append(
                await repo.create_next_telemetry_partition(conn, start=start, end=end)
            )
    return names


async def enforce_retention(app_pool: asyncpg.Pool) -> list[str]:
    """DETACH+DROP partitions whose range ended > RETENTION_DAYS ago.

    Idempotent: the 0040 function returns NULL for already-absent
    partitions and refuses anything inside the retention window.
    """
    today = datetime.now(UTC).date()
    dropped: list[str] = []
    async with app_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.relname
            FROM pg_class c
            JOIN pg_inherits i ON c.oid = i.inhrelid
            WHERE i.inhparent = 'autocomplete_telemetry'::regclass
            """
        )
        for r in rows:
            name: str = r["relname"]
            # autocomplete_telemetry_YYYY_MM → month start
            try:
                y, m = name.rsplit("_", 2)[-2:]
                start = date(int(y), int(m), 1)
            except (ValueError, IndexError):
                continue
            end = _month_start(start.year, start.month + 1).date()
            if (today - end).days <= RETENTION_DAYS:
                continue
            dropped_name = await conn.fetchval(
                "SELECT autocomplete_drop_telemetry_partition($1)", start
            )
            if dropped_name:
                dropped.append(dropped_name)
                logger.warning(
                    "partition_rotation.partition_dropped",
                    extra={"partition": dropped_name, "retention_days": RETENTION_DAYS},
                )
    return dropped


async def rotate(app_pool: asyncpg.Pool) -> tuple[list[str], list[str]]:
    ensured = await ensure_partitions(app_pool)
    dropped = await enforce_retention(app_pool)
    return ensured, dropped


async def run_forever(*, interval_seconds: float = 86400.0) -> None:  # pragma: no cover
    app_pool = await create_pool(
        settings.db_app_role_dsn,
        application_name="autocomplete-service/partition-rotation",
        min_size=1,
        max_size=2,
    )
    try:
        while True:
            try:
                await rotate(app_pool)
            except Exception:  # noqa: BLE001
                logger.exception("partition_rotation.iteration_failed")
            await asyncio.sleep(interval_seconds)
    finally:
        await app_pool.close()


def _main() -> None:  # pragma: no cover — manual ops entrypoint
    """Manual run: uv run --project services/autocomplete-service \
    python -m autocomplete_service.jobs.partition_rotation"""

    async def _run() -> None:
        app_pool = await create_pool(
            settings.db_app_role_dsn,
            application_name="autocomplete-rotation-manual",
            min_size=1,
            max_size=2,
        )
        try:
            ensured, dropped = await rotate(app_pool)
            print(f"ensured: {ensured}")
            print(f"dropped: {dropped or '(none)'}")
        finally:
            await app_pool.close()

    asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    _main()
