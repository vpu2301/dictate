# Sprint-07 — Implementation Plan (as-built)

Verbatim copy of the in-flight TODO list used to drive the sprint. All
items completed unless explicitly carried over (see RETRO).

## Day 1-2 — HF Space scaffolding

- [x] Dockerfile with CUDA base, Python 3.12, Java 21, Keycloak 24.0.5
- [x] supervisord.conf with priority-ordered process tree
- [x] nginx.conf routing /auth/realms/* → Keycloak, others → services
- [x] start.sh: initdb on tmpfs, apply migrations, seed prompts/abbreviations/templates, stage realm-export
- [x] realm-export.json with seeded clinician@demo.test
- [x] .github/workflows/hf-deploy.yml triggered by demo branch push
- [x] _health endpoint that verifies Postgres + Redis + Keycloak liveness

## Day 3-4 — Privacy posture

- [x] `MD_OBJECT_STORE_DISABLED=true` baked into image
- [x] `DEMO_AUDIO_PURGE_ON_FINALIZE=true` baked into image
- [x] `libs/storage` adds `disabled` flag + `ObjectStoreDisabledError`
- [x] sprint-04 finalize handles disabled gracefully (skip audio_files row)
- [x] `scripts/eval/run_daily_privacy_test.py` — login → snapshot → dictate → end → verify clean at 5s + 60s
- [x] `--self-test` mode for deliberate-failure verification
- [x] `.github/workflows/daily-privacy-test.yml` cron 02:00 UTC; pages #privacy-alerts on failure
- [x] Prometheus gauges `mdx_demo_privacy_test_passed` + `mdx_demo_privacy_test_last_run_unix_ts`

## Day 5-6 — WER eval corpus + pipeline

- [x] `eval/corpus/v1/` directory + manifest.json schema + README
- [x] `scripts/eval/check_corpus_pii.py` PII sweep CI gate
- [x] Migration `0015_create_eval_tables.sql` with `eval_runs` + `eval_utterances` + `eval_baseline`
- [x] `scripts/eval/run_wer.py` — Levenshtein WER + CER + RTF + per-category number-norm
- [x] Ukrainian-aware tokenizer (no case-ending stripping)
- [x] Prometheus textfile metrics emitter
- [x] Markdown history at `docs/eval/wer-history/{date}.md`
- [x] `docs/eval/wer-methodology.md`

## Day 7-8 — Nightly schedule + abuse mitigations + observability

- [x] `.github/workflows/nightly-wer.yml` cron 03:00 UTC on self-hosted GPU runner
- [x] `scripts/eval/compare_to_baseline.py` regression gate
- [x] `libs/demo/` package: rate limiter + audit kinds
- [x] Three-axis rate limiter (per-IP concurrent + minutes/h, per-user minutes/24h, session duration cap)
- [x] Cooldown after N hits within window
- [x] Fail-open on Redis errors with WARN log
- [x] 9-test unit suite for rate limiter (all passing)
- [x] `services/auth-service/.../middleware/demo_limit.py` integration
- [x] `monitoring/prometheus/sprint-07-alerts.yml` (WER regressions, RTF, privacy test, rate-limit flood)
- [x] `monitoring/grafana/sprint-07-wer-trend.json`
- [x] `monitoring/grafana/sprint-07-demo-health.json`

## Day 9-10 — ADRs + docs + sign-off

- [x] ADR-0017 — HF Space embedded stack
- [x] ADR-0018 — Demo privacy contract
- [x] ADR-0019 — WER as standing release gate
- [x] `docs/runbooks/hf-space.md`
- [x] `docs/demo/architecture.md`
- [x] `docs/demo/privacy-contract.md`
- [x] `docs/audit/audit-kinds-sprint-07.md`
- [x] Sprint-07 SIGN-OFF.md
- [x] Sprint-07 RETRO.md
- [x] This SPRINT-TODO.md (as-built)
- [x] `project_sprint07.md` auto-memory entry + MEMORY.md index update
