"""Inline completion route — outcome matrix.

Handler exercised directly with a fake state (the sprint-10 telemetry
route test pattern): 200 served, 204 disabled/tenant/timeout/empty/
filtered, 429 rate-limited. The mock inference client lives HERE — no
mock ships in the service (sprint-15 delivery mandate).
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from fastapi import Response
from generation_service.adapters.inference import CompletionResult
from generation_service.config import settings
from generation_service.deps import install_state
from generation_service.domain.slots import SlotPool
from generation_service.routers.completions import (
    InlineCompletionRequest,
    InlineCompletionResponse,
    inline_completion,
)
from pydantic import ValidationError

from auth import Claims

pytestmark = pytest.mark.asyncio

TENANT = uuid4()


def _claims(tid: UUID | None = None) -> Claims:
    return Claims(
        sub=uuid4(), tid=tid or TENANT, roles=["clinician"], scope="openid",
        sid="s", iss="test", aud="mdx-api", exp=2_000_000_000, iat=1,
    )


def _body(text: str = "Пацієнт скаржиться на біль у") -> InlineCompletionRequest:
    return InlineCompletionRequest(
        report_id=uuid4(), section_key="anamnesis", text_before_cursor=text, language="uk"
    )


class _Metric:
    def __init__(self) -> None:
        self.points: list[tuple] = []

    def add(self, value, attrs=None):
        self.points.append(("add", value, attrs))

    def record(self, value, attrs=None):
        self.points.append(("record", value, attrs))


class _MockInference:
    def __init__(self, text: str = "грудній клітці", delay_s: float = 0.0) -> None:
        self.text = text
        self.delay_s = delay_s

    async def complete(self, *, prompt: str, max_tokens: int) -> CompletionResult:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        return CompletionResult(text=self.text, model="gemma3:1b")

    async def ready(self) -> bool:
        return True

    async def aclose(self) -> None:
        pass


class _AllowAllLimiter:
    async def check(self, *, user_id):
        return True, 0


class _DenyLimiter:
    async def check(self, *, user_id):
        return False, 7


class _FakeAuditWriter:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def write_event(self, **kwargs):
        self.events.append(kwargs)


class _FakeShownBuffer:
    def __init__(self) -> None:
        self.recorded: list[UUID] = []

    async def record(self, *, tenant_id: UUID) -> None:
        self.recorded.append(tenant_id)


class _FakeState:
    def __init__(self, inference=None, limiter=None) -> None:
        self.inference = inference if inference is not None else _MockInference()
        self.rate_limiter = limiter or _AllowAllLimiter()
        self.slot_pool = SlotPool(2)
        self.audit_writer = _FakeAuditWriter()
        self.shown_audit = _FakeShownBuffer()
        self.inline_latency_metric = _Metric()
        self.completions_metric = _Metric()


@pytest.fixture(autouse=True)
def _enable_flag(monkeypatch):
    monkeypatch.setattr(settings, "layer_c_enabled", True)
    monkeypatch.setattr(settings, "tenant_allowlist", "")
    monkeypatch.setattr(settings, "gen_timeout_ms", 600)


async def test_served_completion():
    state = _FakeState()
    install_state(state)
    result = await inline_completion(_body(), _claims())
    assert isinstance(result, InlineCompletionResponse)
    assert result.completion == "грудній клітці"
    assert result.model == "gemma3:1b"
    assert result.latency_ms >= 0
    assert state.shown_audit.recorded == [TENANT]
    assert ("add", 1, {"outcome": "served"}) in state.completions_metric.points


async def test_flag_off_is_204(monkeypatch):
    monkeypatch.setattr(settings, "layer_c_enabled", False)
    state = _FakeState()
    install_state(state)
    result = await inline_completion(_body(), _claims())
    assert isinstance(result, Response) and result.status_code == 204


async def test_tenant_not_allowlisted_is_204(monkeypatch):
    monkeypatch.setattr(settings, "tenant_allowlist", str(uuid4()))
    state = _FakeState()
    install_state(state)
    result = await inline_completion(_body(), _claims())
    assert isinstance(result, Response) and result.status_code == 204
    assert ("add", 1, {"outcome": "tenant_disabled"}) in state.completions_metric.points


async def test_allowlisted_tenant_served(monkeypatch):
    monkeypatch.setattr(settings, "tenant_allowlist", str(TENANT))
    state = _FakeState()
    install_state(state)
    result = await inline_completion(_body(), _claims())
    assert isinstance(result, InlineCompletionResponse)


async def test_rate_limited_is_429_with_retry_after():
    state = _FakeState(limiter=_DenyLimiter())
    install_state(state)
    result = await inline_completion(_body(), _claims())
    assert isinstance(result, Response) and result.status_code == 429
    assert result.headers["Retry-After"] == "7"


async def test_timeout_is_204(monkeypatch):
    monkeypatch.setattr(settings, "gen_timeout_ms", 50)
    state = _FakeState(inference=_MockInference(delay_s=0.5))
    install_state(state)
    result = await inline_completion(_body(), _claims())
    assert isinstance(result, Response) and result.status_code == 204
    assert ("add", 1, {"outcome": "timeout"}) in state.completions_metric.points


async def test_slot_exhaustion_times_out(monkeypatch):
    """Both slots held by slow calls → third request 204s within budget."""
    monkeypatch.setattr(settings, "gen_timeout_ms", 100)
    state = _FakeState(inference=_MockInference(delay_s=5.0))
    state.slot_pool = SlotPool(2)
    install_state(state)
    results = await asyncio.gather(
        inline_completion(_body(), _claims()),
        inline_completion(_body(), _claims()),
        inline_completion(_body(), _claims()),
    )
    assert all(isinstance(r, Response) and r.status_code == 204 for r in results)


async def test_empty_completion_is_204():
    state = _FakeState(inference=_MockInference(text="   "))
    install_state(state)
    result = await inline_completion(_body(), _claims())
    assert isinstance(result, Response) and result.status_code == 204
    assert ("add", 1, {"outcome": "empty"}) in state.completions_metric.points


async def test_filtered_completion_is_204_and_audited():
    """A completion introducing '140/90' absent from context → 204 + warn audit."""
    state = _FakeState(inference=_MockInference(text="грудях, АТ 140/90 мм рт.ст."))
    install_state(state)
    result = await inline_completion(_body(), _claims())
    assert isinstance(result, Response) and result.status_code == 204
    assert len(state.audit_writer.events) == 1
    event = state.audit_writer.events[0]
    assert event["kind"] == "layer_c.completion.filtered"
    assert event["payload"]["reason"] == "blood_pressure"
    assert event["payload"]["matched"] == "140/90"
    assert state.shown_audit.recorded == []


async def test_wire_model_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        InlineCompletionRequest(
            report_id=uuid4(), section_key="a", text_before_cursor="x",
            language="uk", extra_field="nope",
        )


async def test_wire_model_rejects_oversized_prefix():
    with pytest.raises(ValidationError):
        _body(text="x" * 1001)


async def test_leading_ellipsis_stripped():
    state = _FakeState(inference=_MockInference(text="...грудній клітці, що посилюється"))
    install_state(state)
    result = await inline_completion(_body(), _claims())
    assert isinstance(result, InlineCompletionResponse)
    assert result.completion == "грудній клітці, що посилюється"
