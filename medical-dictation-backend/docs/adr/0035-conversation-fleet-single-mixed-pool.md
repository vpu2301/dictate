# ADR-0035: Conversation capacity — a single mixed worker pool with weighted caps

Date: 2026-07-26
Status: Accepted
Sprint: 14 (conversation mode — deployment)
Related: ADR-0009 (faster-whisper), ADR-0013 (streaming windowing),
ADR-0019 (WER standing gate), ADR-0021 (pinned offline model bakes),
ADR-0034 (diarization backend)

## Context

Conversation mode (ADR-0034) puts a **second model** — ECAPA + Silero — on
the same device that runs Whisper. That invalidates the sprint-04 capacity
math (4 dictation sessions per worker, spec §9), which assumed one model and
counted sessions by headcount.

Two decisions follow, and this ADR records both:

1. **Fleet shape** — one mixed pool serving both modes, or a dedicated
   conversation pool alongside the dictation pool?
2. **Capacity units** — how much of a worker does a conversation session
   actually consume?

The sprint's own rule was: default to a single mixed pool with weighted
caps (simpler ops) *unless measurements show interference — diarization
spikes degrading dictation latency — then split*. So the decision hangs on
whether interference is real.

## Measurements

Host: Apple M5, **CPU only, no CUDA**, `tiny`/`int8`, macOS, **not
quiesced** (other desktop applications running; load average recorded per
run). Harness: `scripts/eval/run_capacity_probe.py` (`make capacity-probe`),
which drives the production `InferenceQueue` + `StreamingWindower` +
`DiarizationStream` with sessions paced in real time.
Raw: `eval/reports/capacity-probe-cpu.json`,
`eval/reports/capacity-probe-cotenancy.json`.

### Residency (reproducible across all runs, ±2 MB)

| State | Process RSS | Δ |
|---|---|---|
| No models | 203.6 MB | — |
| \+ Whisper resident (tiny/int8) | 563.4 MB | +359.8 MB |
| \+ ECAPA + Silero resident | 729.9 MB | **+166.5 MB** |

Diarization adds **~46 % on top of the Whisper footprint**, not 100 %.
Consistent with ADR-0034's independent figure (ECAPA ≈ 90 MB + Silero ≈ 2 MB
of weights, the rest being torch runtime). Diarizer warmup: 0.8–2.0 s.

### Diarization cost per window (reproducible)

**33–47 ms** p50/p95 across every scenario and every session count —
in line with ADR-0034's independently measured 53–57 ms. Against a Whisper
window of ~480 ms on this host that is **~8 %**, and it did **not** grow
with concurrency.

### Paired co-tenancy trial (the interference question)

Same session count (3), diarization on vs off, run back-to-back, twice:

| Order | Scenario | Whisper p50 | Whisper p95 | Diar p50 |
|---|---|---|---|---|
| 1 | `3d` (3 dictation) | 478.0 ms | 4934.7 ms | — |
| 2 | `2c1d` (2 conv + 1 dict) | 2618.7 / 2870.2 ms | 9467.7 ms | 39.2 ms |
| 3 | `3d` (3 dictation) | 476.0 ms | 3690.5 ms | — |
| 4 | `2c1d` (2 conv + 1 dict) | 394.4 / 499.4 ms | 5673.7 ms | 33.2 ms |

Read this honestly:

- The **dictation-only runs are reproducible** — p50 478.0 and 476.0 ms,
  agreeing to 0.4 % across runs separated in time.
- The **co-tenancy runs are not** — p50 2870 ms then 499 ms, a **6×
  spread between two identical configurations.** In the good trial,
  co-tenancy cost nothing measurable (499 vs 476 ms); in the bad one it
  cost 6×.
- The blow-up is in the **Whisper** path, not the diarizer: diarization
  stayed at 33–47 ms in both trials.

The 3d runs bracketing the bad 2c1d run stayed stable, which points at the
resident torch runtime rather than pure background noise — but the host was
not quiesced and load average climbed 3.2 → 7.0 across the sequence, so this
**cannot be separated from host contention.** An earlier attempt to confirm
the mechanism by pinning `torch.set_num_threads(1)` made things worse, not
better, which further undercuts the simple thread-contention story.

**Absolute latency vs the sprint-04 ≤ 1100 ms partial p95 target is NOT
EVALUABLE on this host** and the probe now refuses to print a verdict there:
this is CPU `tiny`, not A10G `large-v3`, and the earlier full sweep produced
non-monotonic results (3 sessions "faster" than 1), which is proof the run
was noise-dominated rather than a capacity finding. Reporting those numbers
as a pass or a fail either way would be fabrication.

## Decision

### 1. A single mixed pool. Do not split the fleet.

Every dictation-service worker serves both modes. Rationale:

- The evidence for interference is **inconclusive, not positive**. The
  sprint's rule was "split *if* measurements show interference"; a 6×
  spread that does not reproduce, on an unquiesced laptop that cannot
  evaluate the latency target at all, is not that showing.
- Splitting the fleet is the expensive, hard-to-reverse direction: two
  pools, two scaling policies, mode-aware routing, and stranded capacity
  in whichever pool is idle. Merging back later is easy; splitting later
  is also easy. Doing it now on this evidence is not justified.
- The measured *costs* both argue against splitting: +46 % memory (not
  +100 %) and +8 % compute per window, neither growing with concurrency.

This decision is explicitly **provisional on the A10G rig re-run**, which
is a blocking gate before conversation mode reaches real patients (below).

### 2. Weighted capacity, not headcount. Conversation = 2 units.

`MDX_CONVERSATION_SESSION_WEIGHT=2` against a per-worker budget of
`MDX_PER_WORKER_MAX_SESSIONS=4`: **4 dictation OR 2 conversation OR a
2+1+1 mix.** Admission compares *weight*, and the third conversation
session is refused with `gpu_full` (recoverable, close 1013).

Weight 2 is **deliberately conservative** and we are keeping it that way.
On the CPU evidence a conversation session costs ~1.5× a dictation session
on memory and ~1.1× on compute, so weight 2 under-books the worker. That is
the intended direction of error: the failure mode of booking too few
sessions is refused connections that a clinician retries; the failure mode
of booking too many is degraded transcription latency during a live
consultation. Re-tune on the rig, downward only with rig evidence.

Headcount is retired as the capacity signal. It cannot express this: a
worker holding 2 conversation sessions is *full* while
`mdx_dictation_active_sessions` reads "2". The sprint-04
`DictationWorkerSaturated` alert (`active_sessions >= 4`) could not fire in
that state and has been replaced by `DictationWorkerWeightSaturated`.

### 3. Both models warm before the worker advertises conversation capacity.

The diarizer previously loaded lazily on the first conversation session,
which put ~0.8–2.0 s of weight loading inside that session's first window.
It is now warmed at startup (`MDX_DIAR_WARM_AT_STARTUP`, default true).

Warmup failure is **not** fatal: the worker still serves dictation, but
`/readyz` reports `conversation_ready: false` with `diarizer_error`, the
`mdx_dictation_conversation_ready` gauge goes to 0, and conversation
`start_session` is refused with a typed error. A worker never advertises
conversation capacity it cannot honour, and never silently substitutes a
stub that would produce garbage speaker labels.

### 4. Weights are baked, pinned, and re-verified at startup.

ECAPA is baked into both dictation images by an `ecapa-fetch` stage that
reuses `scripts/models/prepare_ecapa.py`, so a developer's
`make prepare-ecapa` and the image produce identical dirs. The digests are
asserted **again at process start** (fail-closed) rather than trusted from
build time — see docs/models/PINS.md § Re-asserted at startup.

## Consequences

- Fleet sizing: **a worker is 4 dictation sessions or 2 consultations.**
  Planning conversation-mode rollout means dividing expected concurrent
  consultations by 2, not by 4.
- Monitoring gained the signals this decision must be judged by, several of
  which **did not previously exist at all**: sprint 04 declared
  `active_sessions`, `partial_latency_ms`, `final_latency_ms`,
  `window_inference_ms` and `rtf` in `metrics.py` and never emitted them, so
  the streaming dashboard and its latency alerts had been querying empty
  series since sprint 04. They are now emitted, split by `mode`.
- If production shows `DictationPartialLatencyHighByMode{mode="dictation"}`
  firing on workers that also carry conversation sessions while
  conversation-free workers stay clean, that is the interference signal this
  ADR could not obtain — and the trigger to revisit the split.

## Open — blocking before conversation mode reaches patients

1. **A10G rig re-run** (`make capacity-probe` + `make der-eval`) with both
   models resident on the GPU: VRAM with Whisper alone vs both, per-window
   combined latency at 1 and 2 conversation sessions, and dictation partial
   p95 with 2 conversation sessions live. **That run, not this one, decides
   whether the sprint-04 ≤ 1100 ms target survives co-tenancy.** The same
   missing rig already blocks the sprint-07 WER gate and the ADR-0034 DER
   gate.
2. **Re-tune `MDX_CONVERSATION_SESSION_WEIGHT`** from the rig numbers.
3. **Whisper startup checksum assertion** — ECAPA is re-verified at startup;
   `MD_ASR_MODEL_SHA256` is still provenance-only.

## What would make us re-open this?

- Rig measurements showing diarization degrading dictation latency on a
  shared worker → split into a dedicated conversation pool.
- VRAM on the A10G proving 2 conversation sessions do not fit alongside the
  large-v3 working set → raise the weight, or split.
- Conversation demand becoming the dominant load → a dedicated pool may win
  on operational simplicity rather than on interference.
