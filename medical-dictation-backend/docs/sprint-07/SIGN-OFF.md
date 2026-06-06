# Sprint 07 — Sign-Off

**Sprint dates:** 2026-05-06 → 2026-05-15
**Status:** ✅ DONE

## Scope delivered

- ✅ Day 1-2: HF Space Dockerfile with embedded Postgres + Redis +
  Keycloak; supervisord process tree; nginx; HF deploy workflow; seeded
  realm-export with `clinician@demo.test`.
- ✅ Day 3-4: Privacy envelope (`MD_OBJECT_STORE_DISABLED`,
  `DEMO_AUDIO_PURGE_ON_FINALIZE`); `ObjectStoreDisabledError`;
  sprint-04 finalize gracefully handles disabled store; daily privacy
  test + GitHub Actions workflow + Prometheus gauges.
- ✅ Day 5-6: WER eval corpus v1 (manifest schema, README, PII sweep);
  migration 0015 (`eval_runs`, `eval_utterances`, `eval_baseline`);
  `scripts/eval/run_wer.py` with Levenshtein + UK-aware tokenisation +
  CER + RTF + per-category number-norm scoring; methodology doc.
- ✅ Day 7-8: Nightly WER GitHub Actions workflow;
  `compare_to_baseline.py` regression gate; `libs/demo` package with
  three-axis rate limiter (per-IP, per-user, per-session) + 9-test
  suite all passing; demo audit kinds set; auth-service middleware;
  Prometheus alert rules; two Grafana dashboards.
- ✅ Day 9-10: ADR-0017 (HF Space embedded stack), ADR-0018 (privacy
  contract), ADR-0019 (WER as standing release gate);
  `docs/runbooks/hf-space.md`; `docs/demo/architecture.md`;
  `docs/demo/privacy-contract.md`; sprint-07 audit kinds doc;
  this sign-off; retro; memory entry.

## Out of scope (deliberate)

- Anonymous demo auth — replaced by real Keycloak + HF API key gating
  per user directive day-1 of the sprint. No `services/auth-service/.../demo/` module
  exists.
- Persistent demo state.
- Adversarial / accent eval corpus (deferred to sprint-08+).
- Load test harness (deferred to sprint-08).

## Tests

- `libs/demo` rate-limit unit tests: **9/9 passing**.
- `libs/storage` (with new `disabled` flag): **7/7 passing**.
- Cumulative sprint 03–07 unit tests: **190 passing**.
- WER pipeline end-to-end smoke: blocked on full corpus authoring
  (linguist consultant ETA: end of sprint-08 week 1). Smoke fixtures
  exercise the pipeline.
- Daily privacy test: dry-run against the freshly built HF Space
  image passed (snapshot empty at 60s).

## Sign-offs

| role                | name          | date        |
| ------------------- | ------------- | ----------- |
| Tech lead           | _pending_     | _pending_   |
| ML/MLOps lead       | _pending_     | _pending_   |
| Security lead       | _pending_     | _pending_   |
| DPO                 | _pending_     | _pending_   |
| Product             | _pending_     | _pending_   |

## Known follow-ups

1. Full 120-utterance WER corpus authoring (linguist consultant).
2. Rotate `clinician@demo.test` password before any public flip.
3. Sprint-08 load-test harness against the HF Space.
4. Establish initial WER baseline after 3 consecutive nightly runs.
