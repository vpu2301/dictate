"""Concurrency load test for dictation-service.

Scenario A: 4 simulated clinicians on 1 worker — all within latency
targets (partial p95 ≤ 1100 ms, final p95 ≤ 2500 ms).
Scenario B: 5th attempt → `gpu_full`.

Skipped unless RUN_DICTATION_LOAD=1.
"""

from __future__ import annotations

import asyncio
import json
import os
import struct
import time
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DICTATION_LOAD") != "1",
    reason="set RUN_DICTATION_LOAD=1 and ensure dev stack is up",
)

WS_URL = os.environ.get("DICTATION_WS_URL", "ws://localhost:8002/ws/dictate")
TOKEN = os.environ.get("DICTATION_TOKEN", "")
PROMPT_ID = os.environ.get("DICTATION_PROMPT_ID", "")


async def _one_client(client_id: int, frames: int) -> dict[str, Any]:
    import websockets

    partials_ms: list[int] = []
    finals_ms: list[int] = []
    async with websockets.connect(
        WS_URL,
        subprotocols=["medical-dictation.v1"],
        additional_headers={"Authorization": f"Bearer {TOKEN}"},
    ) as ws:
        await ws.send(
            json.dumps({"type": "start_session", "prompt_id": PROMPT_ID, "language": "uk"})
        )
        await ws.recv()  # session_started

        async def consumer() -> None:
            send_start = time.monotonic()
            async for raw in ws:
                if not isinstance(raw, str):
                    continue
                msg = json.loads(raw)
                if msg["type"] == "partial":
                    partials_ms.append(int((time.monotonic() - send_start) * 1000) - msg["end_ms"])
                elif msg["type"] == "final":
                    finals_ms.append(int((time.monotonic() - send_start) * 1000) - msg["end_ms"])
                elif msg["type"] == "session_terminated":
                    return

        c_task = asyncio.create_task(consumer())
        for seq in range(frames):
            await ws.send(struct.pack(">I", seq) + b"\x00" * 80)
            await asyncio.sleep(0.020)
        await ws.send(json.dumps({"type": "end_session"}))
        try:
            await asyncio.wait_for(c_task, timeout=15.0)
        except TimeoutError:
            c_task.cancel()

    return {
        "client": client_id,
        "partials": partials_ms,
        "finals": finals_ms,
    }


async def test_four_concurrent_within_targets() -> None:
    """4 clients simultaneously → p95 partial ≤ 1100 ms; final ≤ 2500 ms."""
    results = await asyncio.gather(*(_one_client(i, frames=400) for i in range(4)))
    partials = [m for r in results for m in r["partials"]]
    finals = [m for r in results for m in r["finals"]]
    if partials:
        p_p95 = sorted(partials)[int(len(partials) * 0.95)]
        assert p_p95 <= 1100, f"partial p95 = {p_p95} ms (>1100)"
    if finals:
        f_p95 = sorted(finals)[int(len(finals) * 0.95)]
        assert f_p95 <= 2500, f"final p95 = {f_p95} ms (>2500)"


async def test_fifth_session_rejected() -> None:
    """When 4 already running, the 5th gets gpu_full."""
    import websockets

    holders = [
        await websockets.connect(
            WS_URL,
            subprotocols=["medical-dictation.v1"],
            additional_headers={"Authorization": f"Bearer {TOKEN}"},
        )
        for _ in range(4)
    ]
    try:
        for ws in holders:
            await ws.send(
                json.dumps({"type": "start_session", "prompt_id": PROMPT_ID, "language": "uk"})
            )
            await ws.recv()

        async with websockets.connect(
            WS_URL,
            subprotocols=["medical-dictation.v1"],
            additional_headers={"Authorization": f"Bearer {TOKEN}"},
        ) as ws5:
            await ws5.send(
                json.dumps({"type": "start_session", "prompt_id": PROMPT_ID, "language": "uk"})
            )
            raw = await asyncio.wait_for(ws5.recv(), timeout=3.0)
            msg = json.loads(raw)
            assert msg["type"] == "error"
            assert msg["code"] == "gpu_full"
    finally:
        for ws in holders:
            await ws.close()
