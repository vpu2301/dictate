"""Orchestrator behaviour: idempotence key, pass-through, cache miss/hit.

The orchestrator is the contract for sprint-7's eval harness — same
input + same context → same output, byte-for-byte. These tests gate
that invariant.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any
from uuid import UUID

import pytest

from nlp_service.pipeline.base import (
    AbbreviationSnapshot,
    ProcessingContext,
    Stage,
    StageInput,
    StageOutput,
)
from nlp_service.pipeline.orchestrator import Orchestrator, idempotence_key


class _Identity:
    name = "identity"
    runs_on_partials = True

    async def process(self, ctx: ProcessingContext, input: StageInput) -> StageOutput:
        return StageOutput(text=input.text, words=input.words)


class _Uppercase:
    name = "uppercase"
    runs_on_partials = False  # finals-only

    async def process(self, ctx: ProcessingContext, input: StageInput) -> StageOutput:
        return StageOutput(text=input.text.upper(), words=input.words)


class _InMemoryCache:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    async def set(self, key: str, value: bytes, ttl_seconds: int) -> None:
        self.store[key] = value


def _ctx(is_partial: bool = False) -> ProcessingContext:
    return ProcessingContext(
        tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
        language="uk",
        specialty=None,
        reference_date=date(2026, 1, 1),
        is_partial=is_partial,
        abbreviation_snapshot=AbbreviationSnapshot(entries=(), fingerprint="x"),
        pipeline_version="t",
    )


def test_empty_pipeline_pass_through() -> None:
    orch = Orchestrator(stages=[])
    out = asyncio.run(orch.run(_ctx(), StageInput(text="hello")))
    assert out.text == "hello"


def test_stages_run_in_order() -> None:
    orch = Orchestrator(stages=[_Identity(), _Uppercase()])
    out = asyncio.run(orch.run(_ctx(), StageInput(text="hello")))
    assert out.text == "HELLO"


def test_partials_skip_non_partial_stages() -> None:
    orch = Orchestrator(stages=[_Uppercase()])
    out = asyncio.run(orch.run(_ctx(is_partial=True), StageInput(text="hello")))
    # Uppercase has runs_on_partials=False → skipped.
    assert out.text == "hello"


def test_idempotence_key_stable() -> None:
    a = idempotence_key(_ctx(), StageInput(text="hello"))
    b = idempotence_key(_ctx(), StageInput(text="hello"))
    assert a == b


def test_idempotence_key_changes_with_partial_flag() -> None:
    a = idempotence_key(_ctx(is_partial=False), StageInput(text="hello"))
    b = idempotence_key(_ctx(is_partial=True), StageInput(text="hello"))
    assert a != b


def test_cache_hit_returns_cached_output() -> None:
    cache = _InMemoryCache()
    orch = Orchestrator(stages=[_Uppercase()], cache=cache)
    first = asyncio.run(orch.run(_ctx(), StageInput(text="hello")))
    second = asyncio.run(orch.run(_ctx(), StageInput(text="hello")))
    assert first.text == second.text == "HELLO"
    # Cache populated:
    assert len(cache.store) == 1


def test_pipeline_version_change_invalidates_cache() -> None:
    cache = _InMemoryCache()
    orch = Orchestrator(stages=[_Uppercase()], cache=cache)
    ctx_v1 = _ctx()
    ctx_v2 = ProcessingContext(
        tenant_id=ctx_v1.tenant_id,
        language=ctx_v1.language,
        specialty=ctx_v1.specialty,
        reference_date=ctx_v1.reference_date,
        is_partial=ctx_v1.is_partial,
        abbreviation_snapshot=ctx_v1.abbreviation_snapshot,
        pipeline_version="t-v2",
    )
    asyncio.run(orch.run(ctx_v1, StageInput(text="hello")))
    asyncio.run(orch.run(ctx_v2, StageInput(text="hello")))
    assert len(cache.store) == 2  # different keys
