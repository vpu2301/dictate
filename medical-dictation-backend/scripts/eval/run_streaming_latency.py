"""Synthetic latency probe.

Opens a real WS to dictation-service, streams a fixed reference audio
as 20-ms Opus frames at real-time pace, and records:

- partial latency: time from frame-send to corresponding `partial` reception.
- final latency: time from VAD silence boundary in the audio to `final`.

Targets (sprint-04 spec §9):
  partial p50 ≤ 700 ms   p95 ≤ 1100 ms
  final              p95 ≤ 2500 ms

Emits Prometheus textfile metrics and alerts on > 200 ms regression vs
the 7-day rolling baseline (caller's responsibility — this script just
records).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LatencyResult:
    partial_p50_ms: int
    partial_p95_ms: int
    final_p95_ms: int


def _pct(xs: list[int], p: float) -> int:
    if not xs:
        return 0
    xs_sorted = sorted(xs)
    k = max(0, min(len(xs_sorted) - 1, int(len(xs_sorted) * p)))
    return xs_sorted[k]


async def measure(
    audio_path: Path,
    url: str,
    token: str,
    prompt_id: str,
    language: str,
) -> LatencyResult:
    import websockets

    partials: list[int] = []
    finals: list[int] = []

    async with websockets.connect(
        url,
        subprotocols=["medical-dictation.v1"],
        additional_headers={"Authorization": f"Bearer {token}"},
    ) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "start_session",
                    "prompt_id": prompt_id,
                    "language": language,
                    "target_kind": "generic",
                }
            )
        )
        # First message is session_started; ignore.
        await ws.recv()

        opus_bytes = audio_path.read_bytes()
        # In real harness this would be encoded Opus frames. For now we
        # emulate 50-fps cadence; the actual frame payload is opaque to
        # latency measurement when the audio is pre-recorded.
        frame_count = len(opus_bytes) // 80  # ~80-byte 20-ms frames
        send_start = time.monotonic()
        consumer_task = asyncio.create_task(_consume(ws, partials, finals, send_start))
        for seq in range(frame_count):
            chunk = opus_bytes[seq * 80 : (seq + 1) * 80]
            framed = struct.pack(">I", seq) + chunk
            await ws.send(framed)
            await asyncio.sleep(0.02)
        # Wait for tail finals.
        await asyncio.sleep(3.0)
        await ws.send(json.dumps({"type": "end_session"}))
        try:
            await asyncio.wait_for(consumer_task, timeout=5.0)
        except TimeoutError:
            consumer_task.cancel()

    return LatencyResult(
        partial_p50_ms=_pct(partials, 0.50),
        partial_p95_ms=_pct(partials, 0.95),
        final_p95_ms=_pct(finals, 0.95),
    )


async def _consume(ws: object, partials: list[int], finals: list[int], send_start: float) -> None:
    async for raw in ws:  # type: ignore[union-attr]
        if not isinstance(raw, str):
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        t = msg.get("type")
        if t == "partial":
            elapsed_ms = int((time.monotonic() - send_start) * 1000) - msg["end_ms"]
            partials.append(max(0, elapsed_ms))
        elif t == "final":
            elapsed_ms = int((time.monotonic() - send_start) * 1000) - msg["end_ms"]
            finals.append(max(0, elapsed_ms))
        elif t == "session_terminated":
            return


def emit_prom(result: LatencyResult, path: Path) -> None:
    lines = [
        "# HELP mdx_dictation_synthetic_partial_latency_ms ms",
        "# TYPE mdx_dictation_synthetic_partial_latency_ms gauge",
        f'mdx_dictation_synthetic_partial_latency_ms{{quantile="0.50"}} {result.partial_p50_ms}',
        f'mdx_dictation_synthetic_partial_latency_ms{{quantile="0.95"}} {result.partial_p95_ms}',
        f'mdx_dictation_synthetic_final_latency_ms{{quantile="0.95"}} {result.final_p95_ms}',
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--url", default="ws://localhost:8002/ws/dictate")
    parser.add_argument("--token", required=True)
    parser.add_argument("--prompt-id", required=True)
    parser.add_argument("--language", default="uk")
    parser.add_argument("--metrics-file", type=Path)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = asyncio.run(measure(args.audio, args.url, args.token, args.prompt_id, args.language))
    print(
        f"partial p50={result.partial_p50_ms}ms p95={result.partial_p95_ms}ms  "
        f"final p95={result.final_p95_ms}ms"
    )
    if args.metrics_file:
        args.metrics_file.parent.mkdir(parents=True, exist_ok=True)
        emit_prom(result, args.metrics_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
