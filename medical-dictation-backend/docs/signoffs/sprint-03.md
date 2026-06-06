# Sprint 03 Sign-off — Batch ASR

Cycle: 2026-05-12 → 2026-05-26.
Demo: 2026-05-26.
Composite DoD: see `sprints/backend-todos/sprint-03-whisper-batch-todos.md` § 10.

## Per-reviewer sign-off

| Role                   | Reviewer | Status   | Notes |
| ---------------------- | -------- | -------- | ----- |
| Tech lead              |          | ☐ open   | Code review of every PR; layered architecture preserved. |
| Security lead          |          | ☐ open   | Spec §8 confirmed; envelope correctness; no plaintext at rest. |
| DPO                    |          | ☐ open   | Audio retention, audit completeness, DSAR path supported. |
| ML/MLOps lead          |          | ☐ open   | ADR-0009 signed; WER targets met; model pinned; realtime factor. |
| SRE/DevOps lead        |          | ☐ open   | GPU compose; CPU fallback; runbook; alerts; build budget. |
| Backend engineer A     |          | ☐ open   | `libs/storage` discipline; no direct boto/minio. |
| Backend engineer B     |          | ☐ open   | `libs/crypto` discipline; queue impl. |
| Backend engineer C     |          | ☐ open   | Validators; Whisper engine framing. |

## Verification quick-checks

- [ ] `make ci` green (incl. new gates: `check-no-direct-object-storage.py`,
      `check-no-direct-crypto.py`).
- [ ] `make migrate-up` applies 0006–0009 cleanly; `make migrate-down`
      then `make migrate-up` is a no-op.
- [ ] `make test-integration-db` green; RLS suite includes audio_files
      + transcription_jobs.
- [ ] WER harness: UK general ≤ 18%, UK cardiology ≤ 14%, EN general ≤ 10%,
      EN cardiology ≤ 8% on reference set.
- [ ] Chaos: SIGKILL on worker mid-job — second worker reclaims within
      ≤ 90 s of idle threshold; row eventually marked complete or failed.
- [ ] Master-key chaos: rename `/etc/mdx/master.key`; worker refuses to
      start with clear log pointing to runbook § master-key-missing.
- [ ] OpenAPI snapshot `docs/api/asr-service-openapi.json` in sync.

## Notable deviations from spec

(Capture here as they arise during review — any deviation needs a 1-line
rationale and the ADR/feature ticket where it'll be revisited.)
