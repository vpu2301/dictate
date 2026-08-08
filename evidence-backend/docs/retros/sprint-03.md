# EVA-S03 — Retro (2026-08-05)

**R1 — the ablation harness paid for itself the day it was built.** The
boost-inversion finding (full-strength authority/recency boosts wrecking
nDCG from .79 to .53) would have shipped invisibly without the mode-by-mode
table plus the `rerank_pure` diagnostic mode. Lesson: build the measurement
BEFORE tuning constants; every scoring knob is now ADR'd with the live
evidence attached.

**R2 — CPU budgets vs rig budgets need first-class config.** Three separate
live failures (600 ms connector timeout eaten by the query embed, 400 ms
rerank budget vs 4.5 s CPU reality, cache serving a pre-fix degraded
response) all trace to "spec numbers assumed the rig". Budgets are now env
config with documented dev overrides, and degraded responses are never
cached. Watch for the same class in S04's latency budget (3 s synthesis
start).

**R3 — stale-server verification hazard.** A live check failed against a
server running pre-fix code; twenty minutes went to phantom debugging. Rule
adopted: restart the service AND flush the response cache before any live
verification pass (runbook triage table has the flush one-liner).

**R4 — test premises are code too.** The snapshot-pinning test failed
because MY premise (fixtures-v1 predates v2) was false, not the code. The
corrected test builds its own premise (v3 marker ingested after fixtures-v2
froze). Verification protocols should prefer self-established premises over
remembered history.

**What went well.** The unit-test subagent found a real tokenizer bug
(Latin→Cyrillic range) plus four latent footguns; the freeze suite makes the
connector contract a mechanical fact; eval infrastructure (runs/baseline
tables + protected gates) is the ADR-0019 pattern working end-to-end on day
one.

**Metric.** 1 service, 1 migration, rerank role, 45+11+7 tests, eval harness
with adopted baseline + both-direction gate proof, 2 ADRs' worth of
decisions — one working day.
