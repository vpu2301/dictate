"""Cancelling a job that is already running.

``DELETE /asr/jobs/{id}`` on a RUNNING job cannot stop anything by itself:
asr-service sets ``cancel_requested`` and leaves the status alone. Acting on
it is the worker's job, and before this it only ever looked twice — once
before claiming the message, once after decoding the audio. Anything
cancelled after inference started ran to completion and came back
``complete``, so from the clinician's side the Cancel button did nothing.

These cover the two checkpoints that were missing: between inference chunks,
and the last look before a transcript becomes a fact.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from asr_worker.inference import TranscriptionCancelledError, WhisperEngine
from asr_worker.processor import _cancel_poller


class _FakeEngine(WhisperEngine):
    """A WhisperEngine with the model swapped out.

    VAD and the chunk loop are the parts under test; faster-whisper is not
    (and is not installed on CI's CPU image).
    """

    def __init__(self, chunks: int) -> None:
        super().__init__()
        self._loaded = True
        self.chunks_run = 0
        self._chunks = chunks

    def _run_chunk(self, chunk, language, prompt, offset_ms):  # type: ignore[no-untyped-def]
        self.chunks_run += 1
        return []


def _speech(engine: _FakeEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Force VAD to report N equal speech runs, so the loop has N chunks."""
    from asr_worker import inference as inf
    from asr_worker.vad import SpeechSegment

    segs = [
        SpeechSegment(start_ms=i * 1000, end_ms=(i + 1) * 1000)
        for i in range(engine._chunks)
    ]
    monkeypatch.setattr(inf, "detect_speech", lambda _pcm: segs)


async def test_cancel_between_chunks_stops_inference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _FakeEngine(chunks=5)
    _speech(engine, monkeypatch)
    pcm = np.zeros(16_000 * 5, dtype=np.float32)

    # Cancelled from the third check onwards — the run must stop there, not
    # grind through the remaining chunks.
    calls = {"n": 0}

    async def should_cancel() -> bool:
        calls["n"] += 1
        return calls["n"] >= 3

    with pytest.raises(TranscriptionCancelledError):
        await engine.transcribe(
            pcm, language="uk", prompt=None, should_cancel=should_cancel
        )

    assert engine.chunks_run == 2, "stopped at the checkpoint, not after the last chunk"


async def test_without_a_callback_the_run_is_uninterruptible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The streaming path (transcribe_window) and any caller that does not pass
    # a poller must behave exactly as before.
    engine = _FakeEngine(chunks=3)
    _speech(engine, monkeypatch)
    out = await engine.transcribe(
        np.zeros(16_000 * 3, dtype=np.float32), language="uk", prompt=None
    )
    assert engine.chunks_run == 3
    assert out.language == "uk"


async def test_poller_rate_limits_the_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """VAD can cut a consultation into hundreds of runs; one query each would
    cost more than stopping saves."""
    from asr_worker import processor

    queries = {"n": 0}

    async def fake_is_cancelled(_state, _tenant, _job):  # type: ignore[no-untyped-def]
        queries["n"] += 1
        return False

    monkeypatch.setattr(processor, "_is_cancelled", fake_is_cancelled)
    poll = _cancel_poller(object(), object(), object())

    for _ in range(50):
        assert await poll() is False
    assert queries["n"] == 1, "50 chunks inside one second → one query"


async def test_poller_latches_once_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once the answer is yes it stays yes: the caller is about to raise, and
    a flag that could flap back to False would let inference continue."""
    from asr_worker import processor

    queries = {"n": 0}

    async def fake_is_cancelled(_state, _tenant, _job):  # type: ignore[no-untyped-def]
        queries["n"] += 1
        return True

    monkeypatch.setattr(processor, "_is_cancelled", fake_is_cancelled)
    monkeypatch.setattr(processor, "_CANCEL_POLL_SECONDS", 0.0)
    poll = _cancel_poller(object(), object(), object())

    assert await poll() is True
    assert await poll() is True
    assert queries["n"] == 1


async def test_poller_asks_again_after_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from asr_worker import processor

    queries = {"n": 0}

    async def fake_is_cancelled(_state, _tenant, _job):  # type: ignore[no-untyped-def]
        queries["n"] += 1
        return False

    monkeypatch.setattr(processor, "_is_cancelled", fake_is_cancelled)
    monkeypatch.setattr(processor, "_CANCEL_POLL_SECONDS", 0.01)
    poll = _cancel_poller(object(), object(), object())

    await poll()
    await asyncio.sleep(0.02)
    await poll()
    assert queries["n"] == 2
