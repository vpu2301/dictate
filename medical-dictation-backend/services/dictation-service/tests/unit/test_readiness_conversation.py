"""Sprint-14 deployment: readiness must advertise conversation capacity honestly.

Two independent facts about a worker:

* Can it serve **dictation**? Needs Whisper. If not, ``/readyz`` is 503 and
  the worker should be pulled from the pool.
* Can it serve **conversation**? Needs a WARM diarizer as well. A worker
  that advertises conversation capacity with a cold diarizer pays weight
  loading inside its first window and blows the latency budget — so this
  is reported separately and a dictation-only worker stays 200/ready.

``conversation_slots_free`` is the number the scheduler acts on: it must be
0 whenever the diarizer is cold, regardless of how much raw weight is free.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import Response

from dictation_service.config import settings
from dictation_service.routers.health import readyz
from dictation_service.session.manager import SessionContext, SessionManager


class _FakeConn:
    async def execute(self, _sql: str) -> None:
        return None


class _FakePool:
    def acquire(self) -> Any:
        class _CM:
            async def __aenter__(self) -> _FakeConn:
                return _FakeConn()

            async def __aexit__(self, *_: object) -> None:
                return None

        return _CM()


class _FakeRedis:
    def __init__(self, *, pong: bool = True) -> None:
        self._pong = pong

    async def ping(self) -> bool:
        return self._pong


class _FakeDiarEngine:
    def __init__(self, *, enabled: bool = True, loaded: bool = True, error: str | None = None):
        self.enabled = enabled
        self.loaded = loaded
        self.last_error = error

    @property
    def ready_for_conversation(self) -> bool:
        return self.enabled and self.loaded


def _ctx(*, weight: int) -> SessionContext:
    return SessionContext(
        session_id=uuid4(),
        tenant_id=uuid4(),
        user_id=uuid4(),
        language="uk",
        prompt_id=uuid4(),
        prompt_text="",
        target_kind="generic",
        encounter_id=None,
        template_id=None,
        mode="conversation" if weight > 1 else "dictation",
        capacity_weight=weight,
    )


def _state(
    *,
    whisper_loaded: bool = True,
    diar: _FakeDiarEngine | None = None,
    manager: SessionManager | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        app_pool=_FakePool(),
        redis=_FakeRedis(),
        engine=SimpleNamespace(is_loaded=whisper_loaded),
        diarization_engine=diar or _FakeDiarEngine(),
        session_manager=manager or SessionManager(max_sessions=4),
    )


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _wire(state: SimpleNamespace) -> None:
        monkeypatch.setattr("dictation_service.routers.health.get_state", lambda: state)

    return _wire


async def test_warm_diarizer_advertises_conversation_capacity(wire: Any) -> None:
    state = _state()
    wire(state)
    resp = Response()
    body = await readyz(resp)

    assert resp.status_code == 200
    assert body.status == "ready"
    assert body.diarizer_loaded is True
    assert body.conversation_ready is True
    # 4 weight free / weight 2 per conversation session = 2 slots.
    assert body.conversation_slots_free == 4 // settings.conversation_session_weight


async def test_cold_diarizer_reports_not_ready_for_conversation(wire: Any) -> None:
    """THE gate: the worker is still a healthy dictation worker (200), but
    it must not be handed a conversation session."""
    state = _state(diar=_FakeDiarEngine(loaded=False, error="ModelIntegrityError: missing"))
    wire(state)
    resp = Response()
    body = await readyz(resp)

    assert resp.status_code == 200, "a dictation-only worker is still serving"
    assert body.status == "ready"
    assert body.diarizer_loaded is False
    assert body.conversation_ready is False
    assert body.conversation_slots_free == 0, "must advertise ZERO conversation capacity"
    assert body.diarizer_error is not None, "operator must see WHY without reading logs"


async def test_conversation_disabled_advertises_no_capacity(wire: Any) -> None:
    state = _state(diar=_FakeDiarEngine(enabled=False, loaded=False))
    wire(state)
    body = await readyz(Response())

    assert body.conversation_enabled is False
    assert body.conversation_ready is False
    assert body.conversation_slots_free == 0


async def test_missing_whisper_is_not_ready_at_all(wire: Any) -> None:
    state = _state(whisper_loaded=False)
    wire(state)
    resp = Response()
    body = await readyz(resp)

    assert resp.status_code == 503
    assert body.status == "not_ready"


async def test_capacity_fields_track_weight_not_headcount(wire: Any) -> None:
    """Two conversation sessions = 2 SESSIONS but 4 WEIGHT: the worker is
    full, and readiness must say so."""
    mgr = SessionManager(max_sessions=4)
    await mgr.register(_ctx(weight=2))
    await mgr.register(_ctx(weight=2))
    wire(_state(manager=mgr))
    body = await readyz(Response())

    assert body.capacity_used == 4
    assert body.capacity_max == 4
    assert body.conversation_slots_free == 0


async def test_partially_loaded_worker_reports_remaining_conversation_slots(wire: Any) -> None:
    mgr = SessionManager(max_sessions=4)
    await mgr.register(_ctx(weight=1))  # one dictation session
    wire(_state(manager=mgr))
    body = await readyz(Response())

    assert body.capacity_used == 1
    # 3 weight free // 2 = 1 whole conversation session still fits.
    assert body.conversation_slots_free == 1
