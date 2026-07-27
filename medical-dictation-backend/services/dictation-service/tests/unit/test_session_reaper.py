"""Stale-session reaper.

A worker that dies takes its in-process abandon timers with it, stranding
every session it held in a non-terminal status forever — each one burning a
slot in ``per_tenant_max_active_sessions`` and each one still showing as
"recording" to the clinician. These pin the reaper's one safety interlock:
it collects a session **only** when the owning worker's heartbeat is gone.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest

from dictation_service.session import reaper

TENANT_ID = UUID("00000000-0000-0000-0000-0000000000aa")
USER_ID = UUID("11111111-1111-1111-1111-111111111111")
SESSION_ID = UUID("22222222-2222-2222-2222-222222222222")


def _row(**over: object) -> dict:
    base: dict = {
        "id": SESSION_ID,
        "tenant_id": TENANT_ID,
        "user_id": USER_ID,
        "status": "active",
        "worker_id": "worker-dead",
        "encounter_id": None,
        "last_active_at": None,
    }
    base.update(over)
    return base


class _Manager:
    def __init__(self, held: set[UUID] | None = None) -> None:
        self._held = held or set()

    def get(self, session_id: UUID) -> object | None:
        return object() if session_id in self._held else None


def _make_state(
    monkeypatch: pytest.MonkeyPatch,
    *,
    candidates: list[dict],
    alive_workers: set[str],
    held: set[UUID] | None = None,
    cas_succeeds: bool = True,
) -> tuple[SimpleNamespace, list[dict], list[UUID]]:
    from dictation_service.domain import repository

    audit: list[dict] = []
    abandoned: list[UUID] = []

    async def _list_stale(conn, *, grace_seconds, limit):  # noqa: ANN001
        return candidates

    async def _abandon(conn, *, session_id, expected_status):  # noqa: ANN001
        if not cas_succeeds:
            return False
        abandoned.append(session_id)
        return True

    async def _alive(redis, worker_id):  # noqa: ANN001
        return worker_id in alive_workers

    async def _write_event(**kwargs: object) -> None:
        audit.append(kwargs)

    monkeypatch.setattr(repository, "list_stale_sessions", _list_stale)
    monkeypatch.setattr(repository, "abandon_if_still_stale", _abandon)
    monkeypatch.setattr(reaper, "worker_alive", _alive)

    import contextlib

    @contextlib.asynccontextmanager
    async def _fake_tenant_conn(pool, tenant_id):  # noqa: ANN001
        yield None

    monkeypatch.setattr(reaper, "tenant_connection", _fake_tenant_conn)

    state = SimpleNamespace(
        app_pool=object(),
        redis=object(),
        session_manager=_Manager(held),
        audit_writer=SimpleNamespace(write_event=_write_event),
    )
    return state, audit, abandoned


async def test_dead_workers_sessions_are_collected(monkeypatch: pytest.MonkeyPatch) -> None:
    state, audit, abandoned = _make_state(
        monkeypatch, candidates=[_row()], alive_workers=set()
    )
    reaped = await reaper.reap_tenant(state, TENANT_ID, grace_seconds=300.0)
    assert reaped == 1
    assert abandoned == [SESSION_ID]
    assert audit[0]["payload"]["reason"] == "reaped_dead_worker"
    assert audit[0]["payload"]["prior_status"] == "active"


async def test_a_long_pause_on_a_live_worker_is_never_collected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the heartbeat interlock: a clinician who stepped
    out for an hour must come back to their session, not to a tombstone."""
    state, audit, abandoned = _make_state(
        monkeypatch,
        candidates=[_row(status="paused", worker_id="worker-live")],
        alive_workers={"worker-live"},
    )
    reaped = await reaper.reap_tenant(state, TENANT_ID, grace_seconds=300.0)
    assert reaped == 0
    assert abandoned == []
    assert audit == []


async def test_sessions_held_in_this_process_are_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reaping under a live SessionContext would desync ctx.state from the
    DB — the ctx would keep streaming into an 'abandoned' row."""
    state, _audit, abandoned = _make_state(
        monkeypatch,
        candidates=[_row()],
        alive_workers=set(),
        held={SESSION_ID},
    )
    assert await reaper.reap_tenant(state, TENANT_ID, grace_seconds=300.0) == 0
    assert abandoned == []


async def test_a_session_that_raced_back_to_life_loses_the_cas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, audit, _abandoned = _make_state(
        monkeypatch, candidates=[_row()], alive_workers=set(), cas_succeeds=False
    )
    assert await reaper.reap_tenant(state, TENANT_ID, grace_seconds=300.0) == 0
    assert audit == []


async def test_blank_worker_id_falls_back_to_the_grace_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _audit, abandoned = _make_state(
        monkeypatch, candidates=[_row(worker_id="")], alive_workers=set()
    )
    assert await reaper.reap_tenant(state, TENANT_ID, grace_seconds=300.0) == 1
    assert abandoned == [SESSION_ID]


async def test_audit_failure_does_not_abort_the_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = UUID("33333333-3333-3333-3333-333333333333")
    state, _audit, abandoned = _make_state(
        monkeypatch,
        candidates=[_row(), _row(id=other)],
        alive_workers=set(),
    )

    async def _boom(**kwargs: object) -> None:
        raise RuntimeError("audit down")

    state.audit_writer = SimpleNamespace(write_event=_boom)
    assert await reaper.reap_tenant(state, TENANT_ID, grace_seconds=300.0) == 2
    assert abandoned == [SESSION_ID, other]


async def test_one_bad_tenant_does_not_stop_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good = UUID("00000000-0000-0000-0000-0000000000bb")
    state, _audit, abandoned = _make_state(
        monkeypatch, candidates=[_row()], alive_workers=set()
    )

    async def _tenants(_state, _grace):  # noqa: ANN001
        return [TENANT_ID, good]

    calls: list[UUID] = []
    real_reap = reaper.reap_tenant

    async def _reap(st, tenant_id, *, grace_seconds):  # noqa: ANN001
        calls.append(tenant_id)
        if tenant_id == TENANT_ID:
            raise RuntimeError("pool exhausted")
        return await real_reap(st, tenant_id, grace_seconds=grace_seconds)

    monkeypatch.setattr(reaper, "_tenants_with_stale_sessions", _tenants)
    monkeypatch.setattr(reaper, "reap_tenant", _reap)

    assert await reaper.sweep_once(state) == 1
    assert calls == [TENANT_ID, good]


async def test_reaper_loop_stops_promptly(monkeypatch: pytest.MonkeyPatch) -> None:
    from dictation_service.config import settings

    monkeypatch.setattr(settings, "session_reaper_interval_s", 30.0)
    state = SimpleNamespace()
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(reaper.reaper_loop(state, stop), timeout=1.0)
