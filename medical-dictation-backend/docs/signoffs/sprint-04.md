# Sprint 04 Sign-off — Streaming Dictation

Cycle: 2026-05-26 → 2026-06-09.
Demo: 2026-06-09.
Composite DoD: `sprints/backend-todos/sprint-04-streaming-asr-todos.md` § 10.

## Per-reviewer sign-off

| Role            | Reviewer | Status | Notes |
| --------------- | -------- | ------ | ----- |
| Tech lead       |          | ☐ open | Code review; layered architecture; state-machine branch coverage. |
| Security lead   |          | ☐ open | Threat-model §sprint-04; WS upgrade auth + rate-limit; tmpfs hygiene; uniform-failure resume. |
| Frontend lead   |          | ☐ open | Protocol parity with FE; IndexedDB ring contract; cross-browser reconnect. |
| ML/MLOps lead   |          | ☐ open | Streaming WER within 1 pt of batch; per-window inference p95 ≤ 200 ms; hallucination guard. |
| SRE/DevOps      |          | ☐ open | Dashboard / alerts; nightly synthetic latency; runbook. |
| Backend eng A   |          | ☐ open | `libs/storage` discipline; no direct boto/minio. |
| Backend eng B   |          | ☐ open | Session manager + finalize idempotence. |
| Backend eng C   |          | ☐ open | Opus + tmpfs lifecycle + gap policy. |

## Verification quick-checks

- [ ] `make ci` green incl. import-linter contracts for `dictation_service`.
- [ ] `make migrate-up` applies 0010 cleanly; down + up is a no-op.
- [ ] Sprint-04 unit suite: 45 tests passing (codec / state / aligner / committer / gap / resume).
- [ ] Chaos suite (`RUN_DICTATION_CHAOS=1`) passes all 7 scenarios.
- [ ] Load test (`RUN_DICTATION_LOAD=1`): 4 concurrent sessions within latency targets; 5th rejected with `gpu_full`.
- [ ] Streaming WER: UK general ≤ 19%, UK cardiology ≤ 15%, EN general ≤ 11%, EN cardiology ≤ 9%.
- [ ] Synthetic latency: partial p50 ≤ 700 ms, p95 ≤ 1100 ms; final p95 ≤ 2500 ms.
- [ ] Pen-test checklist (OWASP WS-specific) green.
- [ ] OpenAPI snapshot `docs/api/dictation-service-openapi.json` in sync.
- [ ] Master-key chaos: rename `/etc/mdx/master.key`; service refuses startup pointing to runbook.

## Notable deviations from spec

(Capture as they arise during review.)
