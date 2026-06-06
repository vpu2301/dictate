# Sprint 10 — Retro

## What went well

- **Latency-first design held up.** The trie + cache + lock + lazy
  invalidation pattern is mechanically simple and meets the budget.
  The per-key lock pattern from sprint-08's diff cache transferred
  cleanly to here.
- **PII scrubber centralised once.** Both telemetry intake AND
  phrase-write rejection use the same module. A future pattern
  update lands in one place, not two.
- **Three-scope visibility via two RLS policies.** PERMISSIVE
  `tenant_visibility` (SELECT) + RESTRICTIVE `write_user_phrases`
  (INSERT/UPDATE) cleanly express the system/tenant/user model
  without conditional branching in the service code.
- **Diversity guard prevented the test failure we'd otherwise have
  shipped.** Our first ranking test had three near-identical phrases;
  the diversity filter correctly collapsed them. Indicates the guard
  is doing real work.

## What was hard

- **The diversity guard's Levenshtein threshold (3) is sensitive.**
  Too aggressive and we lose alternatives; too lax and near-dups
  surface. Sprint-10 picks 3 as a documented tradeoff; sprint-future
  may tune from pilot data.
- **`marisa-trie` listed in pyproject but the sprint-10 implementation
  uses Python dicts.** We kept marisa as a dependency for the future
  swap path (ADR-0025 trigger condition). Mild over-spec.
- **Telemetry buffer's flush behaviour was non-trivial to test.** Sprint-10
  ships unit tests of the buffer mechanics; the production flush is
  exercised in load test.

## What we would change

- **Move the GUC-setting boilerplate into a `tenant_connection` variant
  that accepts user_id + role.** Sprint-08 introduced
  `tenant_connection`; sprint-10 needs `app.user_id` + `app.user_role`
  on every connection for the `write_user_phrases` policy. Inlining
  three `set_config` calls in every route is repetitive; an
  `authed_tenant_connection(pool, claims)` helper would clean this up.
- **Author the production-grade corpus in sprint-10 itself, not as a
  follow-up.** The starter corpus exercises the structural anchor but
  the eyeball test needs real phrases.

## Decisions taken

- Trie + Redis cache is the latency floor pattern for autocomplete
  going forward (ADR-0025).
- Bayesian Beta(1,9) prior is the calibrated starting point;
  re-tunable from pilot data.
- PII scrubber is shared between telemetry intake AND write rejection.
- Telemetry is partitioned monthly; 90-day retention; no RLS on the
  table (documented exception).

## Carry-over items

- Sprint-11 (patients): potential PII scrubber regex updates.
- Sprint-12 (notes), 13 (anamnesis), 14 (conversation) consume the
  same `/suggest` endpoint with their own `context.field` values.
- Sprint-15 (generative): Layer C completion.
- Sprint-16: telemetry archive to cold storage past 90 days.
- Sprint-17: admin UI for phrase + snippet management.
