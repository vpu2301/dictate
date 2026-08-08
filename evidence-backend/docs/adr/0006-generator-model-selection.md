# ADR-0006 — Generator selection: the Gemma 3 family for `generator.*`

- **Date:** 2026-08-06
- **Status:** Accepted (clinical countersign of synthesis quality pending)
- **Deciders:** tech lead, EVA-S04

## Context

S04 activates two gateway roles that S00 designed for but left empty:
`generator.fast` (triage classification, intent extraction — small, frequent,
JSON-constrained) and `generator.heavy` (clinical synthesis — the answer
itself). `docs/models/PINS.md` recorded the candidate set as Gemma 3 vs
Kimi K2 and deferred the choice to this sprint.

Non-negotiables: open weights, self-hosted (LM1), multilingual with genuine
Ukrainian competence, constrained JSON decode for the fast role, and a serving
footprint the pilot rig can actually carry alongside BGE-M3 and the
cross-encoder.

## Decision

**Gemma 3 instruct, two sizes:**

| Role | Dev pin | Rig pin | Why |
|---|---|---|---|
| `generator.fast` | `google/gemma-3-4b-it` | same | Triage + intent are short, schema-constrained tasks. 4B is enough and keeps the per-question overhead near zero |
| `generator.heavy` | `google/gemma-3-12b-it` | `google/gemma-3-27b-it` | Synthesis carries the clinical burden. 27B on the rig; 12B is the developer-machine pin so the pipeline is exercisable without the rig |

Serving is a **separate process** (llama-server in dev, vLLM on the rig)
behind the gateway, not in-process like the embedder — generation and
embedding have different memory and batching profiles, and coupling their
lifecycles would make either one's restart the other's outage.

**Kimi K2 is rejected for the pilot** on serving budget: it needs multi-GPU
serving the pilot rig does not have, and the capability gap does not justify
the infrastructure step for an answer that is verified downstream (S06)
rather than trusted.

Two enforcement details the gateway owns rather than the caller:

- **Temperature is clamped** to `EVA_GATEWAY_MAX_TEMPERATURE` (0.2, rule LM2)
  and the clamped value is echoed back, so `answer_provenance.model_pins`
  records what was actually used rather than what was asked for.
- **Readiness asserts generation** when the roles are enabled (BE6): a
  registered role with an unreachable backend makes the gateway honestly
  unready instead of serving 503s that look like bugs.

## Consequences

- The platform already runs Gemma 3 for inline completions
  (generation-service, ADR-0036 there), so the operational knowledge —
  llama-server quirks, the chat-turn wrapper that stops greedy repetition
  loops — transfers directly.
- One model family for both roles means one weights bake, one prompt idiom,
  and one set of failure modes to learn.
- 27B on the rig shares the GPU pool with BGE-M3 and the reranker; capacity
  planning is a deployment-sprint item, not settled here.
- The dev/rig pin split means synthesis *quality* measured on a laptop is not
  the number that matters — the standing gate runs on the rig (rule T4).

## Alternatives considered

- **Kimi K2** — rejected above; revisit when the serving budget changes.
- **Qwen 2.5 / 3 instruct** — comparable open-weight multilingual family and a
  genuine contender. Gemma 3 wins on platform continuity (already served,
  already understood) rather than on a measured quality gap. If the eval
  seed shows Ukrainian synthesis weakness, this is the first thing to try.
- **A medical fine-tune** (Meditron-class) — rejected for now: the pipeline
  supplies evidence in-context and forbids uncited claims, so domain recall
  baked into weights buys less here than instruction-following and citation
  discipline, which general instruct models do better.
- **One model for both roles** — rejected: triage/intent run on every question
  and a 27B forward pass for a five-token JSON classification wastes the rig.

## Trigger conditions for revisiting

- The S06 verification gates (citation faithfulness ≥ 97%) fail on the rig
  with Gemma 3 27B after prompt iteration.
- Ukrainian synthesis quality measurably trails English on the vignette set.
- Multi-GPU serving becomes available, making Kimi K2 (or a larger Gemma)
  affordable.
- A new open-weight release changes the size/quality frontier materially —
  the registry is role-addressed, so this is a config change plus a
  standing-gate run.
