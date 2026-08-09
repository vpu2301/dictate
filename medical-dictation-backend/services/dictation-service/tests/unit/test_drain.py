"""Sprint-16 deployment — scale-in drain semantics.

The contract KEDA/K8s rely on: a draining worker admits nothing new
(clients get the gpu_full reconnect semantics), keeps its live sessions
to completion, reports 503 on /readyz (Service stops routing), and the
internal drain surface is loopback-only.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response

from dictation_service.routers import internal as internal_router
from dictation_service.routers.health import readyz
from dictation_service.session.manager import (
    CapacityError,
    SessionContext,
    SessionManager,
)


def _ctx(weight: int = 1) -> SessionContext:
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


async def test_drain_refuses_new_sessions_keeps_live_ones() -> None:
    mgr = SessionManager(max_sessions=4)
    live = _ctx()
    await mgr.register(live)

    mgr.begin_drain()
    assert mgr.draining
    assert not mgr.fits(1)
    with pytest.raises(CapacityError, match="draining"):
        await mgr.register(_ctx())

    # The live session is untouched and finishes normally.
    assert mgr.total_count == 1
    assert await mgr.unregister(live.session_id) is live
    assert mgr.total_count == 0


async def test_drain_flips_readiness_not_liveness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Conn:
        async def execute(self, _sql: str) -> None: ...

    class _Pool:
        def acquire(self) -> Any:
            class _CM:
                async def __aenter__(self) -> _Conn:
                    return _Conn()

                async def __aexit__(self, *_: object) -> None: ...

            return _CM()

    class _Redis:
        async def ping(self) -> bool:
            return True

    class _Diar:
        enabled = False
        loaded = False
        last_error = None
        ready_for_conversation = False

    mgr = SessionManager(max_sessions=4)
    state = SimpleNamespace(
        app_pool=_Pool(),
        redis=_Redis(),
        engine=SimpleNamespace(is_loaded=True),
        diarization_engine=_Diar(),
        session_manager=mgr,
    )
    monkeypatch.setattr("dictation_service.routers.health.get_state", lambda: state)

    resp = Response()
    assert (await readyz(resp)).status == "ready"

    mgr.begin_drain()
    resp2 = Response()
    body = await readyz(resp2)
    assert resp2.status_code == 503
    assert body.status == "not_ready"


async def test_internal_drain_is_loopback_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr = SessionManager(max_sessions=4)
    state = SimpleNamespace(session_manager=mgr)
    monkeypatch.setattr("dictation_service.routers.internal.get_state", lambda: state)

    def _req(host: str) -> Any:
        return SimpleNamespace(client=SimpleNamespace(host=host))

    with pytest.raises(HTTPException) as exc:
        await internal_router.begin_drain(_req("10.42.0.7"))
    assert exc.value.status_code == 403
    assert not mgr.draining

    out = await internal_router.begin_drain(_req("127.0.0.1"))
    assert out.draining is True
    status = await internal_router.drain_status(_req("::1"))
    assert status.draining is True and status.active_sessions == 0
