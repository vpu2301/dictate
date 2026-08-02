"""Telemetry intake — scrub-before-buffer, 422 matrix, DB-down 204.

The handler is exercised directly (deps installed with fakes): what
matters is that (a) PII is redacted BEFORE the row enters the buffer —
unscrubbed text never exists beyond the request scope — and (b) shape
violations 422 while infra failures never surface to the client.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from autocomplete_service.deps import install_state
from autocomplete_service.routers.telemetry import TelemetryRequest, receive_telemetry
from autocomplete_service.scrubber import REDACTED
from pydantic import ValidationError

from auth import Claims

pytestmark = pytest.mark.asyncio


def _claims() -> Claims:
    return Claims(
        sub=uuid4(), tid=uuid4(), roles=["clinician"], scope="openid",
        sid="s", iss="test", aud="mdx-api", exp=2_000_000_000, iat=1,
    )


class _Metric:
    def add(self, *a, **k):
        pass


class _FakeBuffer:
    def __init__(self) -> None:
        self.rows: list[tuple] = []

    def append(self, row: tuple) -> None:
        self.rows.append(row)


class _FakeState:
    def __init__(self) -> None:
        self.telemetry_buffer = _FakeBuffer()
        self.telemetry_event_metric = _Metric()
        self.telemetry_redaction_metric = _Metric()


# ── shape 422 matrix (model-level: FastAPI turns these into 422) ─────────


def test_unknown_event_rejected():
    with pytest.raises(ValidationError):
        TelemetryRequest(request_id=uuid4(), event="impression", prefix="x")


def test_accepted_without_id_rejected():
    with pytest.raises(ValidationError, match="requires phrase_id or snippet_id"):
        TelemetryRequest(request_id=uuid4(), event="accepted", prefix="x")


def test_both_ids_rejected():
    with pytest.raises(ValidationError, match="mutually exclusive"):
        TelemetryRequest(
            request_id=uuid4(), event="shown_only", prefix="x",
            phrase_id=uuid4(), snippet_id=uuid4(),
        )


def test_prefix_over_wire_cap_rejected():
    with pytest.raises(ValidationError):
        TelemetryRequest(request_id=uuid4(), event="rejected", prefix="x" * 201)


def test_shown_only_without_ids_is_fine():
    TelemetryRequest(request_id=uuid4(), event="shown_only", prefix="x")


# ── scrub-before-buffer ──────────────────────────────────────────────────


async def test_prefix_and_context_scrubbed_before_entering_buffer():
    state = _FakeState()
    install_state(state)
    claims = _claims()
    body = TelemetryRequest(
        request_id=uuid4(),
        event="rejected",
        prefix="пацієнт ivan@example.com тел +380501234567",
        context={"field": "anamnesis", "note": "ІПН 1234567890"},
    )
    resp = await receive_telemetry(body, claims)
    assert resp.status_code == 204
    assert len(state.telemetry_buffer.rows) == 1
    row = state.telemetry_buffer.rows[0]
    prefix_stored, context_stored = row[6], json.loads(row[7])
    assert "ivan@example.com" not in prefix_stored
    assert "380501234567" not in prefix_stored
    assert prefix_stored.count(REDACTED) == 2
    assert "1234567890" not in context_stored["note"]
    # tenant/user come from claims, never the body
    assert row[0] == claims.tid and row[1] == claims.sub


async def test_buffer_failure_never_surfaces():
    class _ExplodingBuffer:
        def append(self, row):
            raise RuntimeError("buffer wedged")

    state = _FakeState()
    state.telemetry_buffer = _ExplodingBuffer()
    install_state(state)
    body = TelemetryRequest(request_id=uuid4(), event="rejected", prefix="зад")
    # Fire-and-forget doctrine: even a wedged buffer must not 5xx.
    resp = await receive_telemetry(body, _claims())
    assert resp.status_code == 204


# ── Sprint 15: Layer C source discriminator ──────────────────────────────


def test_layer_c_accepted_without_ids_is_valid():
    TelemetryRequest(
        request_id=uuid4(), event="accepted", prefix="Пацієнт скарж", source="layer_c"
    )


def test_layer_c_with_phrase_id_rejected():
    with pytest.raises(ValidationError, match="layer_c events must not carry"):
        TelemetryRequest(
            request_id=uuid4(), event="shown_only", prefix="x",
            phrase_id=uuid4(), source="layer_c",
        )


def test_layer_c_with_snippet_id_rejected():
    with pytest.raises(ValidationError, match="layer_c events must not carry"):
        TelemetryRequest(
            request_id=uuid4(), event="rejected", prefix="x",
            snippet_id=uuid4(), source="layer_c",
        )


def test_default_source_is_autocomplete_and_rules_unchanged():
    req = TelemetryRequest(request_id=uuid4(), event="shown_only", prefix="x")
    assert req.source == "autocomplete"
    with pytest.raises(ValidationError, match="requires phrase_id or snippet_id"):
        TelemetryRequest(request_id=uuid4(), event="accepted", prefix="x")


async def test_layer_c_row_carries_source_and_is_scrubbed():
    state = _FakeState()
    install_state(state)
    body = TelemetryRequest(
        request_id=uuid4(),
        event="accepted",
        prefix="пацієнт ivan@example.com диктує",
        source="layer_c",
    )
    resp = await receive_telemetry(body, _claims())
    assert resp.status_code == 204
    row = state.telemetry_buffer.rows[0]
    assert row[8] == "layer_c"
    assert "ivan@example.com" not in row[6]


async def test_autocomplete_row_carries_default_source():
    state = _FakeState()
    install_state(state)
    body = TelemetryRequest(
        request_id=uuid4(), event="accepted", prefix="зад", phrase_id=uuid4()
    )
    await receive_telemetry(body, _claims())
    assert state.telemetry_buffer.rows[0][8] == "autocomplete"
