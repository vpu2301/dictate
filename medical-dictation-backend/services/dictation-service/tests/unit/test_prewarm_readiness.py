"""Sprint 16 — background warmup: /readyz is the traffic gate.

With MDX_WARM_IN_BACKGROUND the lifespan loads Whisper in a thread task
instead of blocking startup. The contract these tests pin: a worker whose
model is still loading answers /healthz (liveness never kills a cold pod)
but /readyz is 503 — the LB sends no traffic before ready — and the flip
to 200 needs nothing beyond ``engine.is_loaded`` turning true.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Response

from dictation_service.routers.health import healthz, readyz
from dictation_service.session.manager import SessionManager


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
    async def ping(self) -> bool:
        return True


class _FakeDiar:
    enabled = False
    loaded = False
    last_error = None
    ready_for_conversation = False


def _state(*, loaded: bool) -> SimpleNamespace:
    return SimpleNamespace(
        app_pool=_FakePool(),
        redis=_FakeRedis(),
        engine=SimpleNamespace(is_loaded=loaded),
        diarization_engine=_FakeDiar(),
        session_manager=SessionManager(max_sessions=4),
    )


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _wire(state: SimpleNamespace) -> None:
        monkeypatch.setattr("dictation_service.routers.health.get_state", lambda: state)

    return _wire


async def test_cold_worker_is_live_but_not_ready(wire: Any) -> None:
    state = _state(loaded=False)
    wire(state)

    # Liveness answers regardless — a cold pod must not be restarted.
    assert (await healthz()).status == "ok"

    resp = Response()
    body = await readyz(resp)
    assert resp.status_code == 503
    assert body.status == "not_ready"
    assert body.model_loaded is False


async def test_readiness_flips_when_model_lands(wire: Any) -> None:
    state = _state(loaded=False)
    wire(state)
    resp = Response()
    assert (await readyz(resp)).status == "not_ready"

    # The background warm task's only observable effect:
    state.engine.is_loaded = True
    resp2 = Response()
    body = await readyz(resp2)
    assert resp2.status_code == 200
    assert body.status == "ready"
