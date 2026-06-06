# ADR-0009 — Inference engine: faster-whisper

**Date:** 2026-05-22
**Status:** Accepted
**Deciders:** ML/MLOps lead, tech lead, security lead

---

## Context

Sprint 03 introduces the first ML inference path in the system. We need
a Whisper backend that:

- Runs the upstream `large-v3` weights without re-training (we have no
  budget for in-house ASR training in the pilot).
- Supports float16 on CUDA — fp32 is 2× slower and gives no quality
  uplift on Whisper.
- Returns per-word timestamps + per-word confidence. Sprint 05's NLP
  consumes them; the UI replay feature (sprint 15) needs them.
- Is maintained, audited, and not abandonware.

Options on the table:

| Engine                     | Notes                                                                |
| -------------------------- | -------------------------------------------------------------------- |
| `faster-whisper` (CTranslate2) | fp16, word-timestamps, beam, condition_on_previous_text, batch. ~4× upstream throughput.  |
| `whisper.cpp`              | CPU-friendly; GPU support via Metal/CUDA but immature on Linux.       |
| OpenAI `whisper`           | Reference implementation; slow, fp32 only on most paths.              |
| NVIDIA NeMo                | More ergonomic for multi-GPU, but heavyweight container and unknown internal model lineage. |

## Decision

Use **`faster-whisper`** ≥ 1.0 on CUDA 12.4 + cuDNN, fp16 compute type,
`large-v3` weights pinned via checksum at container build time.

For the CPU dev fallback (engineers without GPUs), use the same library
with `tiny` model + `int8` compute type. Same code path, so the dev
loop validates the framing logic.

## Consequences

- **Throughput**: ~5× realtime on a single A10G with `large-v3`; meets
  the spec's realtime-factor capacity model.
- **Vendor lock-in**: low. `faster-whisper` is BSD-licensed and the
  weights are upstream Whisper; we can swap to any Whisper backend
  later via the `WhisperEngine` abstraction.
- **CUDA dependency**: the worker image must match the host's NVIDIA
  driver. CI builds against `cuda:12.4.1`; staging hosts pinned to
  `nvidia-driver-550+`. Pinned and documented in the runbook.
- **Word-level confidence**: derived from `word.probability`; we map
  Whisper's logprob into `[0, 1]`. Sprint 05's NLP postprocessor
  conditions on this.

## Alternatives considered

- `whisper.cpp` — would have meant building Metal + CUDA backends
  separately. Rejected: too much surface area for the pilot.
- OpenAI `whisper` — fp16 instability on long audio (>10 min), and
  ~3× slower than faster-whisper. Rejected.
- Build per-clinician fine-tuned models — out of scope; revisit
  post-pilot once we have ≥ 10 hours of clinician audio with consent.

## Trigger conditions for revisiting

- CUDA 12.x sunset by faster-whisper.
- WER regression > 2 points on a faster-whisper minor bump.
- Need for multilingual same-session (sprint 04 streaming) that the
  engine can't satisfy.
