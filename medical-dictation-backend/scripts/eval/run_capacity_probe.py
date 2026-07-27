"""Dual-model capacity probe for conversation mode (sprint 14 deployment).

Answers the three questions the sprint-14 deployment EXPLORE asks, on the
actual inference host:

  1. Resident memory with **Whisper only** (baseline).
  2. Resident memory with **both models** (Whisper + ECAPA + Silero).
  3. **Per-window combined latency** (Whisper wait + diarization) and
     **partial latency** at N concurrent conversation sessions, plus the
     co-tenancy mix (conversation sessions alongside dictation ones).

It drives the PRODUCTION code path, not a re-implementation:

  * ``InferenceQueue``  — the single per-worker queue that serialises every
    ``transcribe_window`` call across sessions (this is what makes a second
    session cost latency rather than throughput).
  * ``StreamingWindower`` — the real 4 s / 2 s windower + committer.
  * ``DiarizationStream`` — the real VAD → ECAPA → clustering stream.

Sessions are paced in **real time**: each has an audio clock that advances
at wall-clock rate, exactly like a live WebSocket feeding the ring buffer.
``partial_age_ms`` is therefore computed the same way the WS handler does
(``buffer.total_ms - partial.end_ms``), so the p95 here is comparable to
the sprint-04 target of ≤ 1100 ms.

Device memory: on CUDA hosts this reports ``torch.cuda.max_memory_allocated``
alongside process RSS. On CPU hosts (this laptop) VRAM does not exist and the
script reports RSS only — clearly labelled. Per the ADR-0019 / ADR-0034
precedent, laptop numbers are the plumbing + relative signal; the A10G rig
re-run is the release gate.

Usage:
    uv run python scripts/eval/run_capacity_probe.py
    uv run python scripts/eval/run_capacity_probe.py --scenarios 1c,2c,2c1d
    uv run python scripts/eval/run_capacity_probe.py --json out.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "services" / "dictation-service" / "src"))

# Same macOS auto-selection as run_wer.py / run_der.py: no CUDA here, so fall
# back to tiny/int8 on CPU. An explicit MD_ASR_* env var always wins.
if platform.system() == "Darwin":
    os.environ.setdefault("MD_ASR_DEVICE", "cpu")
    os.environ.setdefault("MD_ASR_COMPUTE_TYPE", "int8")
    os.environ.setdefault("MD_ASR_MODEL", "tiny")
# The probe is a measurement harness, not a service: no DB, no Redis, no OTLP.
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

import numpy as np  # noqa: E402

SAMPLE_RATE = 16_000
DEFAULT_CORPUS = REPO_ROOT / "eval" / "conversations" / "v1"
DEFAULT_ECAPA_DIR = Path.home() / ".cache" / "mdx-models" / "ecapa-voxceleb"

# Sprint-04 §9 target the co-tenancy run must not break.
PARTIAL_P95_TARGET_MS = 1100.0


# ── memory ───────────────────────────────────────────────────────────


def _rss_mb() -> float:
    """Resident set size of this process, in MB."""
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KB, macOS reports bytes.
    return peak / 1024.0 / 1024.0 if platform.system() == "Darwin" else peak / 1024.0


def _current_rss_mb() -> float:
    """Current (not peak) RSS — peak overstates steady-state residency."""
    try:
        import psutil  # type: ignore[import-not-found]

        return float(psutil.Process().memory_info().rss) / 1024.0 / 1024.0
    except Exception:
        pass
    if platform.system() == "Darwin":
        import subprocess

        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(os.getpid())],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if out.isdigit():
            return float(out) / 1024.0
    try:
        with open(f"/proc/{os.getpid()}/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) / 1024.0
    except Exception:
        pass
    return _rss_mb()


def _vram_mb() -> float | None:
    """Allocated VRAM in MB, or None on a host without CUDA."""
    try:
        import torch

        if not torch.cuda.is_available():
            return None
        return float(torch.cuda.memory_allocated()) / 1024.0 / 1024.0
    except Exception:
        return None


def _loadavg() -> list[float]:
    """1/5/15-minute load average. A busy host inflates every latency
    number here, so it is recorded alongside them rather than assumed
    away — this is a laptop, not a quiesced rig."""
    try:
        return [round(v, 2) for v in os.getloadavg()]
    except (OSError, AttributeError):
        return []


def _device_report() -> dict:
    return {
        "rss_mb": round(_current_rss_mb(), 1),
        "vram_mb": (lambda v: round(v, 1) if v is not None else None)(_vram_mb()),
        "loadavg": _loadavg(),
    }


# ── corpus ───────────────────────────────────────────────────────────


def _load_wav(path: Path) -> np.ndarray:
    import wave

    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == SAMPLE_RATE, f"{path}: not 16 kHz"
        assert w.getnchannels() == 1, f"{path}: not mono"
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _fit_to_seconds(pcm: np.ndarray, seconds: float) -> np.ndarray:
    """Trim or tile a clip to the requested session length.

    Real consultations run minutes; the corpus dialogues are ~25 s. Tiling
    lets a scenario collect enough windows for a p95 that means something
    (a 10-sample p95 is just the max).
    """
    want = int(seconds * SAMPLE_RATE)
    if pcm.shape[0] >= want:
        return pcm[:want]
    reps = -(-want // pcm.shape[0])  # ceil
    return np.tile(pcm, reps)[:want]


def _corpus_audio(corpus: Path) -> list[tuple[str, np.ndarray]]:
    out: list[tuple[str, np.ndarray]] = []
    for d in sorted(p for p in corpus.iterdir() if p.is_dir()):
        wav = d / "audio.wav"
        if wav.exists():
            out.append((d.name, _load_wav(wav)))
    if not out:
        raise SystemExit(f"no dialogues with audio.wav under {corpus}")
    return out


# ── a simulated session ──────────────────────────────────────────────


@dataclass
class SessionProbe:
    """One simulated live session on this worker.

    ``mode`` is 'conversation' (Whisper + diarization) or 'dictation'
    (Whisper only) — the two loads whose co-tenancy this sprint measures.
    """

    name: str
    mode: str
    audio: np.ndarray
    windower: object
    diarization: object | None = None

    whisper_ms: list[float] = field(default_factory=list)
    diar_ms: list[float] = field(default_factory=list)
    combined_ms: list[float] = field(default_factory=list)
    partial_age_ms: list[float] = field(default_factory=list)
    windows: int = 0

    @property
    def duration_ms(self) -> int:
        return int(self.audio.shape[0] * 1000 // SAMPLE_RATE)


async def _run_session(
    probe: SessionProbe,
    queue: object,
    *,
    tick_interval_s: float,
    started_at: float,
    trace: bool = False,
) -> None:
    """Replay one session in real time through the production window loop.

    Mirrors ``ws/handler._window_loop``: tick, take the next slice of the
    audio that has 'arrived' so far, submit to the shared inference queue,
    integrate, then diarize the same window in a thread.
    """
    windower = probe.windower
    while True:
        await asyncio.sleep(tick_interval_s)
        # The audio clock: how much audio a live WS would have buffered.
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        buffer_total_ms = min(elapsed_ms, probe.duration_ms)
        slice_ = windower.next_slice(buffer_total_ms=buffer_total_ms)  # type: ignore[attr-defined]
        if slice_ is None:
            if buffer_total_ms >= probe.duration_ms:
                return
            continue
        pcm = probe.audio[
            slice_.start_ms * SAMPLE_RATE // 1000 : slice_.end_ms * SAMPLE_RATE // 1000
        ]
        if pcm.shape[0] == 0:
            continue

        t_window = time.monotonic()
        prev_text = windower.build_prompt_for_next_window()  # type: ignore[attr-defined]
        t0 = time.monotonic()
        result = await queue.submit(  # type: ignore[attr-defined]
            pcm, language="uk", prompt=None, prev_text=prev_text
        )
        whisper_ms = (time.monotonic() - t0) * 1000.0
        tick = windower.integrate(  # type: ignore[attr-defined]
            window_segments=getattr(result, "segments", []),
            window_no_speech_prob=getattr(result, "no_speech_prob", 1.0),
            window_start_ms=slice_.start_ms,
            window_end_ms=slice_.end_ms,
            infer_seconds=whisper_ms / 1000.0,
            pcm_for_vad=pcm,
        )

        diar_ms = 0.0
        if probe.mode == "conversation" and probe.diarization is not None:
            t1 = time.monotonic()
            await asyncio.to_thread(
                probe.diarization.process_window,  # type: ignore[attr-defined]
                pcm,
                window_start_ms=slice_.start_ms,
            )
            diar_ms = (time.monotonic() - t1) * 1000.0

        probe.windows += 1
        probe.whisper_ms.append(whisper_ms)
        probe.diar_ms.append(diar_ms)
        probe.combined_ms.append((time.monotonic() - t_window) * 1000.0)

        age_ms: float | None = None
        if tick.new_partial is not None:
            # Same formula as the WS handler: how stale the partial is
            # relative to the audio the client has already sent.
            now_ms = min(int((time.monotonic() - started_at) * 1000), probe.duration_ms)
            age_ms = float(max(0, now_ms - tick.new_partial.end_ms))
            probe.partial_age_ms.append(age_ms)

        if trace:
            print(
                f"    [{probe.name}] win {slice_.start_ms:>6}-{slice_.end_ms:>6}"
                f"  whisper {whisper_ms:>7.0f} ms  diar {diar_ms:>5.0f} ms"
                f"  partial_age {age_ms if age_ms is not None else '-'}"
            )

        if slice_.end_ms >= probe.duration_ms:
            return


# ── scenarios ────────────────────────────────────────────────────────


def _p(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def _summarise(probes: list[SessionProbe]) -> dict:
    def agg(key: str, mode: str | None = None) -> list[float]:
        return [
            v
            for p in probes
            if mode is None or p.mode == mode
            for v in getattr(p, key)
        ]

    out: dict = {
        "sessions": [{"name": p.name, "mode": p.mode, "windows": p.windows} for p in probes],
        "windows_total": sum(p.windows for p in probes),
    }
    for label, mode in (("all", None), ("conversation", "conversation"), ("dictation", "dictation")):
        combined = agg("combined_ms", mode)
        if not combined:
            continue
        partials = agg("partial_age_ms", mode)
        out[label] = {
            "whisper_ms_p50": round(_p(agg("whisper_ms", mode), 0.50), 1),
            "whisper_ms_p95": round(_p(agg("whisper_ms", mode), 0.95), 1),
            "diar_ms_p50": round(_p(agg("diar_ms", mode), 0.50), 1),
            "diar_ms_p95": round(_p(agg("diar_ms", mode), 0.95), 1),
            "combined_ms_p50": round(_p(combined, 0.50), 1),
            "combined_ms_p95": round(_p(combined, 0.95), 1),
            "combined_ms_max": round(max(combined), 1),
            "partial_age_ms_p50": round(_p(partials, 0.50), 1),
            "partial_age_ms_p95": round(_p(partials, 0.95), 1),
            "partial_samples": len(partials),
        }
    return out


async def _scenario(
    spec: str,
    *,
    engine: object,
    diar_engine: object | None,
    audio: list[tuple[str, np.ndarray]],
    tick_interval_s: float,
    max_seconds: float,
    trace: bool = False,
) -> dict:
    """Run one scenario. Spec is like '2c1d' = 2 conversation + 1 dictation."""
    from dictation_service.inference import InferenceQueue
    from dictation_service.inference.windower import StreamingWindower

    n_conv, n_dict = _parse_spec(spec)
    probes: list[SessionProbe] = []
    for i in range(n_conv + n_dict):
        mode = "conversation" if i < n_conv else "dictation"
        name, pcm = audio[i % len(audio)]
        clip = _fit_to_seconds(pcm, max_seconds)
        probes.append(
            SessionProbe(
                name=f"{mode[:4]}-{i}-{name}",
                mode=mode,
                audio=clip,
                windower=StreamingWindower(base_prompt="", language="uk"),
                diarization=(
                    diar_engine.new_stream()  # type: ignore[attr-defined]
                    if mode == "conversation" and diar_engine is not None
                    else None
                ),
            )
        )

    queue = InferenceQueue(
        transcribe_window_fn=engine.transcribe_window,  # type: ignore[attr-defined]
        deadline_multiplier=1.5,
        worker_id="capacity-probe",
    )
    mem_before = _device_report()
    t0 = time.monotonic()
    async with queue:
        started = time.monotonic()
        await asyncio.gather(
            *(
                _run_session(
                    p,
                    queue,
                    tick_interval_s=tick_interval_s,
                    started_at=started,
                    trace=trace,
                )
                for p in probes
            )
        )
    wall_s = time.monotonic() - t0

    result = _summarise(probes)
    result["scenario"] = spec
    result["conversation_sessions"] = n_conv
    result["dictation_sessions"] = n_dict
    result["capacity_weight"] = n_conv * 2 + n_dict
    result["wall_seconds"] = round(wall_s, 1)
    result["audio_seconds_per_session"] = round(probes[0].duration_ms / 1000.0, 1)
    result["memory_before"] = mem_before
    result["memory_after"] = _device_report()
    return result


def _parse_spec(spec: str) -> tuple[int, int]:
    """'2c1d' -> (2, 1); '4d' -> (0, 4); '1c' -> (1, 0)."""
    n_conv = n_dict = 0
    num = ""
    for ch in spec.lower():
        if ch.isdigit():
            num += ch
        elif ch == "c":
            n_conv += int(num or "1")
            num = ""
        elif ch == "d":
            n_dict += int(num or "1")
            num = ""
        else:
            raise SystemExit(f"bad scenario spec {spec!r}; use e.g. 2c1d, 4d, 1c")
    if n_conv + n_dict == 0:
        raise SystemExit(f"bad scenario spec {spec!r}: no sessions")
    return n_conv, n_dict


# ── main ─────────────────────────────────────────────────────────────


def _print_block(title: str) -> None:
    print(f"\n{'─' * 72}\n{title}\n{'─' * 72}")


async def amain(args: argparse.Namespace) -> int:
    from asr_worker.inference import WhisperEngine
    from dictation_service.diarization.engine import DiarizationEngine

    host = f"{platform.system()} {platform.machine()} / py{platform.python_version()}"
    print(f"host: {host}")
    print(
        f"asr:  model={os.environ.get('MD_ASR_MODEL', '(config default)')} "
        f"device={os.environ.get('MD_ASR_DEVICE', '(config default)')} "
        f"compute={os.environ.get('MD_ASR_COMPUTE_TYPE', '(config default)')}"
    )
    has_cuda = _vram_mb() is not None
    print(f"cuda: {'yes' if has_cuda else 'NO — RSS only, VRAM columns are n/a'}")

    report: dict = {
        "host": host,
        "cuda": has_cuda,
        "asr_model": os.environ.get("MD_ASR_MODEL", "(config default)"),
        "asr_device": os.environ.get("MD_ASR_DEVICE", "(config default)"),
        "asr_compute_type": os.environ.get("MD_ASR_COMPUTE_TYPE", "(config default)"),
        "diar_model_dir": str(args.ecapa_dir),
        "scenarios": [],
    }

    # ── Phase 1: memory floor before any model ───────────────────────
    _print_block("Phase 1 — residency")
    empty = _device_report()
    print(f"  process, no models        RSS {empty['rss_mb']:>8.1f} MB   loadavg {empty['loadavg']}")

    # ── Phase 2: Whisper resident (baseline) ─────────────────────────
    engine = WhisperEngine()
    engine.load()
    whisper_only = _device_report()
    print(
        f"  + Whisper resident        RSS {whisper_only['rss_mb']:>8.1f} MB"
        f"   (Δ {whisper_only['rss_mb'] - empty['rss_mb']:+.1f} MB)"
    )

    # ── Phase 3: both models resident ────────────────────────────────
    if args.diar_torch_threads:
        import torch

        torch.set_num_threads(args.diar_torch_threads)
        print(f"  torch intra-op threads pinned to {args.diar_torch_threads}")

    diar_engine = DiarizationEngine(
        model_dir=str(args.ecapa_dir), device=args.diar_device, enabled=True
    )
    t0 = time.monotonic()
    await diar_engine.ensure_loaded()
    diar_warm_s = time.monotonic() - t0
    both = _device_report()
    print(
        f"  + ECAPA + Silero resident RSS {both['rss_mb']:>8.1f} MB"
        f"   (Δ {both['rss_mb'] - whisper_only['rss_mb']:+.1f} MB)"
    )
    print(f"  diarizer warmup           {diar_warm_s * 1000:.0f} ms")
    if has_cuda:
        print(
            f"  VRAM: whisper {whisper_only['vram_mb']} MB → both {both['vram_mb']} MB"
        )

    report["residency"] = {
        "no_models": empty,
        "whisper_only": whisper_only,
        "both_models": both,
        "diarizer_delta_mb": round(both["rss_mb"] - whisper_only["rss_mb"], 1),
        "diarizer_warmup_ms": round(diar_warm_s * 1000, 0),
    }

    # ── Phase 4: latency per scenario ────────────────────────────────
    audio = _corpus_audio(args.corpus)
    for spec in args.scenarios.split(","):
        spec = spec.strip()
        if not spec:
            continue
        _print_block(f"Phase 4 — scenario {spec}")
        res = await _scenario(
            spec,
            engine=engine,
            diar_engine=diar_engine,
            audio=audio,
            tick_interval_s=args.tick_ms / 1000.0,
            max_seconds=args.session_seconds,
            trace=args.trace,
        )
        report["scenarios"].append(res)
        n_conv, n_dict = res["conversation_sessions"], res["dictation_sessions"]
        print(
            f"  {n_conv} conversation + {n_dict} dictation "
            f"(capacity weight {res['capacity_weight']}/4), "
            f"{res['audio_seconds_per_session']} s audio each, "
            f"{res['windows_total']} windows in {res['wall_seconds']} s wall"
        )
        for label in ("conversation", "dictation"):
            if label not in res:
                continue
            m = res[label]
            print(
                f"    {label:<13} whisper p50/p95 {m['whisper_ms_p50']:>7.1f}/{m['whisper_ms_p95']:>7.1f} ms"
                f" | diar p50/p95 {m['diar_ms_p50']:>5.1f}/{m['diar_ms_p95']:>5.1f} ms"
                f" | combined p95 {m['combined_ms_p95']:>7.1f} ms"
                f" | partial age p95 {m['partial_age_ms_p95']:>7.1f} ms"
                f" ({m['partial_samples']} partials)"
            )
        print(
            f"    memory after: RSS {res['memory_after']['rss_mb']:.1f} MB"
            f"   loadavg {res['memory_before']['loadavg']} → {res['memory_after']['loadavg']}"
        )

    # ── Verdicts ─────────────────────────────────────────────────────
    _print_block("Verdict")

    # 1. The diarization cost — measurable on any host, and the number the
    #    fleet decision actually turns on: does the second model add a
    #    small constant, or does it spike?
    diar_all = [
        v
        for res in report["scenarios"]
        if "conversation" in res
        for v in (res["conversation"]["diar_ms_p50"], res["conversation"]["diar_ms_p95"])
    ]
    if diar_all:
        print(
            f"  diarization cost/window across scenarios: "
            f"{min(diar_all):.0f}–{max(diar_all):.0f} ms  (p50s and p95s)"
        )

    # 2. The latency target. On a host without CUDA this is NOT evaluable:
    #    absolute latency is set by CPU Whisper, not by the design. Saying
    #    "OVER" here would be reporting a failure the run cannot establish.
    if has_cuda:
        for res in report["scenarios"]:
            for label in ("conversation", "dictation"):
                if label not in res:
                    continue
                p95 = res[label]["partial_age_ms_p95"]
                verdict = "PASS" if p95 <= PARTIAL_P95_TARGET_MS else "OVER"
                print(
                    f"  {res['scenario']:<6} {label:<13} partial p95 {p95:>8.1f} ms "
                    f"vs {PARTIAL_P95_TARGET_MS:.0f} ms target — {verdict}"
                )
    else:
        print(
            f"\n  partial-latency vs the {PARTIAL_P95_TARGET_MS:.0f} ms target: NOT EVALUABLE on this host.\n"
            "  This is a CPU laptop running tiny/int8, not the A10G + large-v3 the\n"
            "  target was set for, and it is not quiesced — check the loadavg above.\n"
            "  Treat any partial_age number here as host noise, not as a result:\n"
            "  if the per-scenario figures are NON-MONOTONIC in session count\n"
            "  (e.g. 3 sessions 'faster' than 1), contention with other processes\n"
            "  dominated the run and even the relative signal is unusable.\n"
            "  What IS usable from this host: model residency and the diarization\n"
            "  cost per window. The A10G rig re-run is the release gate\n"
            "  (ADR-0019 / ADR-0034 precedent)."
        )

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--ecapa-dir", type=Path, default=DEFAULT_ECAPA_DIR)
    parser.add_argument("--diar-device", default=os.environ.get("MDX_DIAR_DEVICE", "cpu"))
    parser.add_argument(
        "--scenarios",
        default="1c,2c,2c1d,4d",
        help="comma-separated, e.g. '1c,2c,2c1d,4d' (c=conversation, d=dictation)",
    )
    parser.add_argument("--session-seconds", type=float, default=25.0)
    parser.add_argument("--tick-ms", type=int, default=600)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument(
        "--trace", action="store_true", help="print every window's timing (interference hunting)"
    )
    parser.add_argument(
        "--diar-torch-threads",
        type=int,
        default=0,
        help="pin torch intra-op threads for the diarizer (0 = torch default). "
        "On CPU hosts torch's pool fights the ASR engine for cores.",
    )
    args = parser.parse_args()
    if not args.ecapa_dir.exists():
        raise SystemExit(
            f"ECAPA model dir not found: {args.ecapa_dir}\nRun: make prepare-ecapa"
        )
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
