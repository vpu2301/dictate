"""Step-05 integration — roll-up idempotence + partition retention.

Skipped unless ``RUN_DB_INTEGRATION=1``. Uses a synthetic day far in
the past so the tests never collide with live dev telemetry, and
restores every artifact it creates.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg
import pytest
from autocomplete_service.jobs.partition_rotation import enforce_retention
from autocomplete_service.jobs.rollup import rollup_all

from db import create_pool

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_INTEGRATION") != "1",
    reason="set RUN_DB_INTEGRATION=1 to run; needs `make dev-up && make migrate-up`",
)

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))
DB_NAME = os.environ.get("POSTGRES_DB", "medical_dictation")
SU_DSN = f"postgresql://postgres:postgres@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
APP_DSN = f"postgresql://app_role:app_role@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
AUDIT_DSN = (
    f"postgresql://audit_writer:audit_writer@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
)

TENANT_A = UUID("00000000-0000-0000-0000-00000000000a")


class CountingRedis:
    def __init__(self) -> None:
        self.incrs: list[str] = []

    async def incr(self, key: str) -> int:
        self.incrs.append(key)
        return len(self.incrs)


async def test_rollup_idempotent_single_vtag_bump_and_greatest():
    """Seed one accepted event on a synthetic past day → roll up twice:
    counters bump once, vtag INCR exactly once, and last_accepted_at is
    NOT regressed by the old-day run (GREATEST, migration 0040)."""
    from audit import AuditWriter

    day = date(2026, 6, 15)  # inside the seeded 2026_06 partition
    su = await asyncpg.connect(SU_DSN)
    phrase_id = await su.fetchval(
        "SELECT id FROM autocomplete_phrases WHERE source='system' AND language='uk' "
        "ORDER BY phrase LIMIT 1"
    )
    baseline = await su.fetchrow(
        "SELECT impression_count, acceptance_count, last_accepted_at "
        "FROM autocomplete_phrases WHERE id=$1", phrase_id
    )
    newer_accept = datetime(2026, 7, 1, tzinfo=UTC)
    try:
        # Give the phrase a NEWER last_accepted_at than the synthetic day —
        # the old-day roll-up must not move it backwards.
        await su.execute(
            "UPDATE autocomplete_phrases SET last_accepted_at=$2, "
            "impression_count=impression_count+1, acceptance_count=acceptance_count+1 "
            "WHERE id=$1",
            phrase_id, newer_accept,
        )
        await su.execute(
            "INSERT INTO autocomplete_telemetry "
            "(tenant_id, user_id, request_id, event_type, phrase_id, prefix_scrubbed, "
            " context_jsonb, created_at) "
            "VALUES ($1, $2, $3, 'accepted', $4, 'itest', '{}', $5)",
            TENANT_A, uuid4(), uuid4(), phrase_id,
            datetime(2026, 6, 15, 12, 0, tzinfo=UTC),
        )

        app_pool = await create_pool(APP_DSN, application_name="rollup-itest",
                                     min_size=1, max_size=2)
        audit_pool = await create_pool(AUDIT_DSN, application_name="rollup-itest-a",
                                       min_size=1, max_size=2)
        redis = CountingRedis()
        writer = AuditWriter(audit_pool)
        try:
            n1 = await rollup_all(app_pool=app_pool, audit_writer=writer,
                                  redis=redis, day=day)
            n2 = await rollup_all(app_pool=app_pool, audit_writer=writer,
                                  redis=redis, day=day)  # idempotent re-run
        finally:
            await app_pool.close()
            await audit_pool.close()

        assert n1 == 1 and n2 == 0, f"roll-up not idempotent: {n1=} {n2=}"
        assert len(redis.incrs) == 1, "vtag bumped more than once per (tenant, day)"

        after = await su.fetchrow(
            "SELECT impression_count, acceptance_count, last_accepted_at "
            "FROM autocomplete_phrases WHERE id=$1", phrase_id
        )
        assert after["impression_count"] == baseline["impression_count"] + 2
        assert after["acceptance_count"] == baseline["acceptance_count"] + 2
        # GREATEST: the June roll-up must not regress the July timestamp.
        assert after["last_accepted_at"] == newer_accept
    finally:
        await su.execute(
            "DELETE FROM autocomplete_telemetry WHERE prefix_scrubbed='itest'"
        )
        await su.execute(
            "DELETE FROM autocomplete_rollup_progress WHERE rollup_date=$1", day
        )
        await su.execute(
            "UPDATE autocomplete_phrases SET impression_count=$2, acceptance_count=$3, "
            "last_accepted_at=$4 WHERE id=$1",
            phrase_id, baseline["impression_count"], baseline["acceptance_count"],
            baseline["last_accepted_at"],
        )
        await su.close()


async def test_bump_counters_never_stores_infinity():
    """Impressions-only bump (p_accepts=0, p_last_accepted=NULL) on a
    never-accepted row must leave last_accepted_at NULL — not the
    '-infinity' GREATEST sentinel (migration 0041). A real accept
    timestamp then applies, and GREATEST still never moves it backwards."""
    su = await asyncpg.connect(SU_DSN)
    phrase_id = await su.fetchval(
        "SELECT id FROM autocomplete_phrases WHERE source='system' AND language='uk' "
        "ORDER BY phrase LIMIT 1"
    )
    baseline = await su.fetchrow(
        "SELECT impression_count, acceptance_count, last_accepted_at "
        "FROM autocomplete_phrases WHERE id=$1", phrase_id
    )
    accept_ts = datetime(2026, 7, 1, tzinfo=UTC)
    try:
        await su.execute(
            "UPDATE autocomplete_phrases SET last_accepted_at=NULL WHERE id=$1",
            phrase_id,
        )
        app_pool = await create_pool(APP_DSN, application_name="bump-itest",
                                     min_size=1, max_size=1)
        try:
            async with app_pool.acquire() as conn:
                # impressions but zero accepts on a NULL row → stays NULL
                found = await conn.fetchval(
                    "SELECT autocomplete_bump_phrase_counters($1, 3, 0, NULL)",
                    phrase_id,
                )
                assert found is True
                assert await su.fetchval(
                    "SELECT last_accepted_at IS NULL FROM autocomplete_phrases "
                    "WHERE id=$1", phrase_id
                ), "impressions-only bump must not store '-infinity'"

                # real accept → timestamp lands
                await conn.fetchval(
                    "SELECT autocomplete_bump_phrase_counters($1, 1, 1, $2)",
                    phrase_id, accept_ts,
                )
                assert await su.fetchval(
                    "SELECT last_accepted_at FROM autocomplete_phrases WHERE id=$1",
                    phrase_id,
                ) == accept_ts

                # older accept (stale roll-up re-run) → GREATEST holds
                await conn.fetchval(
                    "SELECT autocomplete_bump_phrase_counters($1, 1, 1, $2)",
                    phrase_id, accept_ts - timedelta(days=10),
                )
                assert await su.fetchval(
                    "SELECT last_accepted_at FROM autocomplete_phrases WHERE id=$1",
                    phrase_id,
                ) == accept_ts
        finally:
            await app_pool.close()
    finally:
        await su.execute(
            "UPDATE autocomplete_phrases SET impression_count=$2, acceptance_count=$3, "
            "last_accepted_at=$4 WHERE id=$1",
            phrase_id, baseline["impression_count"], baseline["acceptance_count"],
            baseline["last_accepted_at"],
        )
        await su.close()


async def test_retention_drops_only_expired_partitions():
    """A synthetic >90-day-old partition is dropped; current ones survive;
    a re-run is a no-op."""
    su = await asyncpg.connect(SU_DSN)
    old_start = date.today().replace(day=1) - timedelta(days=200)
    old_start = old_start.replace(day=1)
    name = f"autocomplete_telemetry_{old_start.strftime('%Y_%m')}"
    try:
        await su.execute(
            f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF autocomplete_telemetry "
            f"FOR VALUES FROM ('{old_start.isoformat()}') "
            f"TO ('{(old_start + timedelta(days=32)).replace(day=1).isoformat()}')"
        )
        app_pool = await create_pool(APP_DSN, application_name="retention-itest",
                                     min_size=1, max_size=1)
        try:
            # S16 changed the return shape to (dropped, archived); archiving
            # is flag-gated off in this test, so archived stays empty.
            dropped1, _archived1 = await enforce_retention(app_pool)
            dropped2, _archived2 = await enforce_retention(app_pool)  # idempotent
        finally:
            await app_pool.close()

        assert name in dropped1, f"expired partition not dropped: {dropped1}"
        assert dropped2 == [], "second run must be a no-op"
        remaining = {
            r["relname"]
            for r in await su.fetch(
                "SELECT c.relname FROM pg_class c JOIN pg_inherits i "
                "ON c.oid=i.inhrelid WHERE i.inhparent='autocomplete_telemetry'::regclass"
            )
        }
        assert name not in remaining
        # current-month partition survived
        cur = f"autocomplete_telemetry_{date.today().strftime('%Y_%m')}"
        assert cur in remaining
    finally:
        await su.execute(f"DROP TABLE IF EXISTS {name}")
        await su.close()


async def test_layer_c_rows_ignored_by_phrase_counters_but_feed_acceptance_rate():
    """Sprint 15: layer_c telemetry rides the same table but (a) never bumps
    phrase counters and (b) feeds the acceptance-rate gauge state."""
    from autocomplete_service.jobs import rollup as rollup_mod

    from audit import AuditWriter

    day = date(2026, 6, 16)  # inside the seeded 2026_06 partition
    su = await asyncpg.connect(SU_DSN)
    phrase_id = await su.fetchval(
        "SELECT id FROM autocomplete_phrases WHERE source='system' AND language='uk' "
        "ORDER BY phrase LIMIT 1"
    )
    baseline = await su.fetchrow(
        "SELECT impression_count, acceptance_count FROM autocomplete_phrases WHERE id=$1",
        phrase_id,
    )
    ts = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)
    try:
        # 3 shown, 1 accepted, 1 rejected — all layer_c, no phrase ids.
        for event, n in (("shown_only", 3), ("accepted", 1), ("rejected", 1)):
            for _ in range(n):
                await su.execute(
                    "INSERT INTO autocomplete_telemetry "
                    "(tenant_id, user_id, request_id, event_type, prefix_scrubbed, "
                    " context_jsonb, source, created_at) "
                    "VALUES ($1, $2, $3, $4, 'itest-lc', '{}', 'layer_c', $5)",
                    TENANT_A, uuid4(), uuid4(), event, ts,
                )
        app_pool = await create_pool(APP_DSN, application_name="itest-lc", min_size=1, max_size=2)
        audit_pool = await create_pool(AUDIT_DSN, application_name="itest-lc-audit", min_size=1, max_size=2)
        redis = CountingRedis()
        try:
            await rollup_all(
                app_pool=app_pool, audit_writer=AuditWriter(audit_pool),
                redis=redis, day=day,
            )
        finally:
            await app_pool.close()
            await audit_pool.close()

        after = await su.fetchrow(
            "SELECT impression_count, acceptance_count FROM autocomplete_phrases WHERE id=$1",
            phrase_id,
        )
        assert after["impression_count"] == baseline["impression_count"]
        assert after["acceptance_count"] == baseline["acceptance_count"]
        events = rollup_mod.layer_c_events_by_type()
        assert events == {"shown_only": 3, "accepted": 1, "rejected": 1}
        assert rollup_mod.layer_c_acceptance_rate() == pytest.approx(1 / 5)
    finally:
        await su.execute(
            "DELETE FROM autocomplete_telemetry WHERE prefix_scrubbed='itest-lc'"
        )
        await su.execute(
            "DELETE FROM autocomplete_rollup_progress WHERE rollup_date=$1", day
        )
        await su.close()
