"""Sprint-14 deployment: weighted capacity as the WS handler enforces it.

``test_capacity_weighted.py`` covers the SessionManager arithmetic. This
covers the thing an operator actually sees: what the handler does on the
wire when the budget is gone. The sprint-14 deployment VERIFY is precisely
"caps admit 4 dictation OR 2 conversation OR the measured mix; the 3rd
conversation session → ``gpu_full``".

The refusal must be RECOVERABLE (close 1013 "try again later") — a client
retries when another session ends; it is not a client error.

Everything below the handler is faked, same approach as
``test_conversation_start_gate.py``: the real admission logic runs, the DB
and OS resources do not.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from auth import Claims
from dictation_service.config import settings
from dictation_service.domain import consents as consents_mod
from dictation_service.domain import encounters as encounters_mod
from dictation_service.domain import repository as repository_mod
from dictation_service.protocol.messages import StartSession, StartSessionV2
from dictation_service.session.manager import SessionManager
from dictation_service.ws import handler as handler_mod
from dictation_service.ws.handler import _new_session
from dictation_service.ws.upgrade import UpgradeContext

TENANT_ID = uuid4()
USER_ID = uuid4()
PROMPT_ID = uuid4()


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed_with: list[int] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def close(self, code: int = 1000) -> None:
        self.closed_with.append(code)

    def frames(self) -> list[dict[str, Any]]:
        return [json.loads(t) for t in self.sent]


class _FakeAuditWriter:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def write_event(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


class _FakeDiarizationStream:
    segments: list[Any] = []

    def attribute(self, start_ms: int, end_ms: int) -> tuple[str | None, float | None]:
        return None, None


class _FakeDiarizationEngine:
    async def ensure_loaded(self) -> None:
        return None

    def new_stream(self) -> _FakeDiarizationStream:
        return _FakeDiarizationStream()


class _FakeBuffer:
    total_ms = 0

    def __init__(self, *, session_id: UUID) -> None:
        self.session_id = session_id

    def close(self) -> None:
        return None


def _claims() -> Claims:
    now = int(time.time())
    return Claims(
        sub=USER_ID,
        tid=TENANT_ID,
        roles=["clinician"],
        sid="sess-1",
        iss="https://kc.example/realms/mdx",
        aud="medical-dictation",
        exp=now + 3600,
        iat=now,
    )


def _upgrade(*, protocol_version: int) -> UpgradeContext:
    return UpgradeContext(
        claims=_claims(),
        subprotocol=("medical-dictation.v2" if protocol_version == 2 else "medical-dictation.v1"),
        client_ip="127.0.0.1",
        origin=None,
        protocol_version=protocol_version,
        bearer="tok",
    )


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    @asynccontextmanager
    async def _fake_tenant_connection(_pool: Any, _tenant_id: UUID) -> Any:
        yield SimpleNamespace()

    async def _count_active(_conn: Any, *, tenant_id: UUID) -> int:
        return 0

    async def _fetch_encounter_status(_conn: Any, *, encounter_id: UUID) -> str | None:
        return "in_progress"

    async def _fetch_recording_consent(_conn: Any, *, encounter_id: UUID) -> dict[str, Any] | None:
        return {"patient_id": uuid4(), "consent_id": uuid4()}

    async def _insert_session(_conn: Any, **kwargs: Any) -> None:
        return None

    async def _fetch_prompt_text(_state: Any, _tid: UUID, _pid: UUID) -> str | None:
        return "медичний контекст"

    monkeypatch.setattr(handler_mod, "tenant_connection", _fake_tenant_connection)
    monkeypatch.setattr(repository_mod, "count_active_for_tenant", _count_active)
    monkeypatch.setattr(repository_mod, "insert_session", _insert_session)
    monkeypatch.setattr(encounters_mod, "fetch_encounter_status", _fetch_encounter_status)
    monkeypatch.setattr(consents_mod, "fetch_recording_consent", _fetch_recording_consent)
    monkeypatch.setattr(handler_mod, "_fetch_prompt_text", _fetch_prompt_text)
    monkeypatch.setattr(handler_mod, "SessionAudioBuffer", _FakeBuffer)
    monkeypatch.setattr(handler_mod, "OpusDecoder", lambda: SimpleNamespace())

    return SimpleNamespace(
        app_pool=SimpleNamespace(),
        session_manager=SessionManager(max_sessions=4),
        audit_writer=_FakeAuditWriter(),
        diarization_engine=_FakeDiarizationEngine(),
        engine=SimpleNamespace(model_name="large-v3"),
        template_client=None,
    )


async def _start_conversation(state: SimpleNamespace) -> tuple[Any, _FakeWebSocket]:
    ws = _FakeWebSocket()
    ctx = await _new_session(
        ws,
        _upgrade(protocol_version=2),
        state,
        StartSessionV2(
            prompt_id=PROMPT_ID, language="uk", encounter_id=uuid4(), mode="conversation"
        ),
    )
    return ctx, ws


async def _start_dictation(state: SimpleNamespace) -> tuple[Any, _FakeWebSocket]:
    ws = _FakeWebSocket()
    ctx = await _new_session(
        ws,
        _upgrade(protocol_version=1),
        state,
        StartSession(prompt_id=PROMPT_ID, language="uk"),
    )
    return ctx, ws


def _assert_gpu_full(ws: _FakeWebSocket) -> None:
    frames = ws.frames()
    assert frames, "expected an error frame"
    assert frames[-1]["type"] == "error"
    assert frames[-1]["code"] == "gpu_full"
    # Recoverable: the client retries when a session ends. 1013 = try again later.
    assert frames[-1]["recoverable"] is True
    assert ws.closed_with == [1013]


async def test_four_dictation_sessions_are_admitted_then_the_fifth_is_refused(
    state: SimpleNamespace,
) -> None:
    for _ in range(4):
        ctx, _ = await _start_dictation(state)
        assert ctx is not None
    assert state.session_manager.total_weight == 4

    ctx, ws = await _start_dictation(state)
    assert ctx is None
    _assert_gpu_full(ws)


async def test_two_conversation_sessions_are_admitted_then_the_third_is_refused(
    state: SimpleNamespace,
) -> None:
    """The headline capacity claim: conversation costs weight 2, so the
    worker takes two — and the THIRD gets gpu_full, not a degraded session."""
    for _ in range(2):
        ctx, _ = await _start_conversation(state)
        assert ctx is not None
        assert ctx.capacity_weight == settings.conversation_session_weight

    assert state.session_manager.total_weight == 4
    assert state.session_manager.total_count == 2, "two SESSIONS occupying four slots"

    ctx, ws = await _start_conversation(state)
    assert ctx is None
    _assert_gpu_full(ws)


async def test_the_measured_mix_one_conversation_plus_two_dictation_fits_exactly(
    state: SimpleNamespace,
) -> None:
    conv, _ = await _start_conversation(state)
    assert conv is not None
    for _ in range(2):
        ctx, _ = await _start_dictation(state)
        assert ctx is not None
    assert state.session_manager.total_weight == 4

    # Budget exhausted for BOTH modes.
    ctx, ws = await _start_dictation(state)
    assert ctx is None
    _assert_gpu_full(ws)

    ctx, ws = await _start_conversation(state)
    assert ctx is None
    _assert_gpu_full(ws)


async def test_conversation_is_refused_when_only_one_weight_unit_is_free(
    state: SimpleNamespace,
) -> None:
    """A conversation session needs its WHOLE weight free. Three dictation
    sessions leave one slot — enough for dictation, not for conversation."""
    for _ in range(3):
        ctx, _ = await _start_dictation(state)
        assert ctx is not None
    assert state.session_manager.total_weight == 3

    ctx, ws = await _start_conversation(state)
    assert ctx is None, "must not admit a conversation session into a half-slot"
    _assert_gpu_full(ws)

    # ...but a dictation session still fits.
    ctx, _ = await _start_dictation(state)
    assert ctx is not None
    assert state.session_manager.total_weight == 4


async def test_ending_a_conversation_session_frees_its_whole_weight(
    state: SimpleNamespace,
) -> None:
    conv, _ = await _start_conversation(state)
    assert conv is not None
    assert state.session_manager.total_weight == 2

    await state.session_manager.unregister(conv.session_id)
    assert state.session_manager.total_weight == 0

    # The full budget is available again — 4 dictation sessions, not 2.
    for _ in range(4):
        ctx, _ = await _start_dictation(state)
        assert ctx is not None
    assert state.session_manager.total_weight == 4
