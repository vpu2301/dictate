# ADR-0036: Layer C inline completion — local Gemma behind a provider seam

Date: 2026-08-02
Status: Accepted
Sprint: 15

## Context

Sprint-10 deferred generative completion ("Layer C lives beyond this
service"). The sprint-15 spec presumed it would reuse "the sprint-12
generation-service / Gemma stack" — **which does not exist in this
repo**: sprint 12 shipped notifications (ADR-0029..0031); the only LLM
decision on record is the report-service `Synthesizer` seam with a
deterministic mock default and an Anthropic stub gated on compliance
sign-off. There is also **no GPU rig** — three release gates (WER, DER,
streaming latency) are already blocked on the missing A10G machine.

Layer C's requirements are unlike section synthesis: synchronous, tiny
(≤ 24 tokens), on the typing path (p95 ≤ 400 ms end-to-end, hard 600 ms
→ 204), and running against text the clinician is typing right now
(PHI).

## Decision

1. **New `generation-service` (:8009)** — the forward-referenced home
   for generation workloads. Inline completion is its first entry point.
2. **Local Gemma 3 1B (Q4_K_M GGUF), served by `llama-server`**
   (llama.cpp) beside the service; PHI never leaves the box, matching
   the platform's offline-model doctrine (whisper/ecapa). Pin in
   docs/models/PINS.md. `InferenceClient` is a Protocol (the
   `Synthesizer`/`build_synthesizer` seam shape) with **two real
   backends** — `LlamaCppClient` (default) and `OllamaClient` — selected
   by `MDX_GEN_BACKEND`; the deterministic mock lives in tests only.
3. **Why not Ollama as default**: measured on the dev M5, Ollama 0.32.5
   adds a constant **~420 ms/request scheduler overhead** with gemma3
   (SWA cache forces full prompt re-processing), alone consuming the
   entire budget. llama-server serves the same GGUF without it.
4. **Why 1B not 4B**: measured 2026-08-02, 24-token greedy completions,
   ~200-token clinical prompt, Apple M5 24 GB:

   | backend | model | mode | p50 | p95 |
   |---|---|---|---|---|
   | Ollama 0.32.5 | gemma3:1b | alone | 929 ms | 1103 ms |
   | Ollama 0.32.5 | gemma3:1b | contended | 1789 ms | 2227 ms |
   | llama.cpp b10210 | gemma3:1b | alone | 583 ms | 676 ms |
   | llama.cpp b10210 | gemma3:1b | contended | 844 ms | 908 ms |
   | Ollama | gemma3:4b | alone | 1690 ms | 2205 ms |

   (`scripts/eval/measure_layer_c_latency.py`; "contended" = one
   concurrent 512-token generation, emulating a synthesis job.)
5. **The p95 ≤ 400 ms budget is a rig-gated release gate** — the fourth
   one (ADR-0035 posture: "reporting a pass/fail on this host would be
   fabrication"). Generation on the M5 runs at 56–73 tok/s; 24 tokens
   alone is ~330–430 ms, so the dev host cannot meet the budget at the
   spec shape. On an A10G-class GPU (200+ tok/s generation, ms-scale
   prompt eval) the budget is comfortably met; the number must be pasted
   from the rig before Layer C ships to production. Dev hosts raise
   `MDX_GEN_TIMEOUT_MS` to keep the feature usable; production keeps 600.
6. **Guardrails** (the model proposes, the clinician disposes):
   completions are ghost text until explicitly accepted; an **output
   safety filter** drops any completion containing numeric clinical
   values / dosages / ICD-like codes not verbatim in
   `text_before_cursor` (204 + `layer_c.completion.filtered`, warn);
   2-slot pool isolates the typing path; dual-window per-user rate limit
   (burst 10/s, 30/10s); `MDX_LAYER_C_ENABLED` kill switch +
   `MDX_GEN_TENANT_ALLOWLIST` per-tenant opt-in; telemetry rides the
   sprint-10 pipeline with `source='layer_c'` and acceptance rate is the
   kill-switch input.
7. **Scope mapping**: the spec's `autocomplete.suggest` scope maps to
   the live `("autocomplete.read", "phrase")` permission (what
   /autocomplete/suggest itself checks) — no new permission.
8. `report_id` in the request is correlation context, **not
   dereferenced**: the completion depends only on the typed text, RLS
   means a foreign id resolves to nothing the caller couldn't see, and a
   DB round-trip would spend budget on nothing.

## Consequences

- A second serving stack (llama-server) joins the fleet; its bake/pin
  flow (fetch at pin → sha256 → bake) is deferred with the GPU rig and
  recorded in PINS.md.
- Silence is a feature: every degraded state (flag off, tenant not
  opted in, timeout, filter, backend error) is a 204, never an error
  the editor must handle.
- Alerts: LayerCInlineLatencyHigh / LayerCFilterRateHigh /
  LayerCBackendErrors (sprint-15-alerts.yml); dashboard
  sprint-15-layer-c-replay-search.
