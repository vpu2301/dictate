"""Partition rotation must cover the CURRENT month, not just the next.

Regression: the original implementation only ever created next month's
partition, so a service that was down (or a job that never ran) over a
month boundary silently dropped every telemetry row for the whole month
— observed live on 2026-07-07 with only 2026-05/06 partitions present.

Step-05 extends the window to current + MONTHS_AHEAD (2) and adds the
90-day retention leg (integration-tested against the real DB).
"""

from __future__ import annotations

from datetime import UTC, datetime

from autocomplete_service.jobs.partition_rotation import (
    MONTHS_AHEAD,
    _partition_bounds,
)


def test_bounds_cover_current_plus_two_months() -> None:
    now = datetime(2026, 7, 7, 16, 44, tzinfo=UTC)
    bounds = _partition_bounds(now)
    assert bounds == [
        (datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC)),
        (datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC)),
        (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)),
    ]


def test_bounds_december_rolls_into_next_year() -> None:
    now = datetime(2026, 12, 15, tzinfo=UTC)
    bounds = _partition_bounds(now)
    assert bounds == [
        (datetime(2026, 12, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC)),
        (datetime(2027, 1, 1, tzinfo=UTC), datetime(2027, 2, 1, tzinfo=UTC)),
        (datetime(2027, 2, 1, tzinfo=UTC), datetime(2027, 3, 1, tzinfo=UTC)),
    ]


def test_bounds_are_contiguous_month_boundaries() -> None:
    for month in range(1, 13):
        now = datetime(2026, month, 15, tzinfo=UTC)
        bounds = _partition_bounds(now)
        assert len(bounds) == MONTHS_AHEAD + 1
        for i, (start, end) in enumerate(bounds):
            assert start.day == end.day == 1
            if i:
                assert start == bounds[i - 1][1]  # contiguous
        assert bounds[0][0] <= now < bounds[0][1]
