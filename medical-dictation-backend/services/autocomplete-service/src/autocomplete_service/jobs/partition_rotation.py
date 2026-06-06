"""Monthly partition rotation for autocomplete_telemetry.

Cron 1st of each month: create the next month's partition; optionally
DETACH partitions older than 90 days (sprint-16 physical archive).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import asyncpg

from db import create_pool

from ..config import settings
from .. import repository as repo

logger = logging.getLogger(__name__)


def _next_month_bounds(now: datetime) -> tuple[datetime, datetime]:
    if now.month == 12:
        start = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
        end   = datetime(now.year + 1, 2, 1, tzinfo=timezone.utc)
    else:
        start = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
        if start.month == 12:
            end = datetime(start.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            end = datetime(start.year, start.month + 1, 1, tzinfo=timezone.utc)
    return start, end


async def ensure_next_partition(app_pool: asyncpg.Pool) -> str:
    now = datetime.now(timezone.utc)
    start, end = _next_month_bounds(now)
    async with app_pool.acquire() as conn:
        return await repo.create_next_telemetry_partition(conn, start=start, end=end)


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
                await ensure_next_partition(app_pool)
            except Exception:  # noqa: BLE001
                logger.exception("partition_rotation.iteration_failed")
            await asyncio.sleep(interval_seconds)
    finally:
        await app_pool.close()
