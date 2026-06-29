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

## Gap-fill (post-sprint, branch S07)

Items above were checked optimistically during the sprint; an as-built
audit found several only partially wired. Closed here:

- [x] **Corpus fixtures** — `eval/corpus/v1/` shipped with an *empty*
  manifest. Added the 8 placeholder fixtures (4 specialties × 2 langs:
  cardiology/endocrinology/radiology/general, `audio.wav` +
  `transcript.txt` + `metadata.json`) and a SHA-256 `manifest.json`
  (`scripts/eval/build_corpus_manifest.py`).
- [x] **`run_wer.py`** — was the sprint-03 `--fixtures` smoke harness
  (WER only). Rebuilt to the methodology contract: `--corpus` mode with
  SHA-256 integrity gate, WER (UK-aware) + CER + RTF p50/p95 +
  per-category number-norm, JSON report + Markdown history + the spec's
  Prometheus metric names, and `--dsn` persistence to
  `audit.eval_runs`/`eval_utterances` (so `compare_to_baseline.py` has
  rows to gate on). Legacy `--fixtures` mode preserved for
  `make wer-eval`. Pure scoring/integrity logic extracted to
  `scripts/eval/wer_lib.py`.
- [x] **Eval unit tests** — `scripts/eval/tests/` (24 tests): UK
  wrong-case WER, CER, number-norm, determinism, manifest tamper/missing
  detection, PII sweep, aggregation + Prometheus emission.
- [x] **CI gate** — `make check-corpus` (integrity + PII + eval tests)
  wired into `ci.yml` gates job and `make ci`.
- [x] **Eval audit kinds** — `scripts/eval/audit_kinds.py`
  (`EVAL_AUDIT_KINDS`); `eval.run.started/completed` emitted by
  `run_wer.py`, `eval.run.regressed` by `compare_to_baseline.py`;
  documented in `event-kinds.md` + `audit-kinds-sprint-07.md`.
- [x] **Demo privacy envelope on the streaming path** — dictation-service
  built `EncryptedObjectStore` *without* `disabled=`, so
  `MD_OBJECT_STORE_DISABLED` was never honored (the demo's real WS path
  would have written audio). Added `object_store_disabled` +
  `demo_audio_purge_on_finalize` config, wired `disabled=` in
  `main_deps.py`, and made `finalize_session` honor `purge_audio`
  (skip persistence + zero the in-memory PCM). Covered by
  `test_finalize_privacy.py` (3 tests).

### Still carried over (unchanged from sprint plan)

- Full 120-utterance corpus authoring (clinical content lead + linguist).
- Initial WER baseline capture after 3 consecutive nightly GPU runs.
- Git-LFS wiring for real corpus audio (placeholders commit as plain
  files; SRE wires LFS for the authored set).
- End-to-end GPU run of `run_wer.py --corpus` (needs the A10G eval rig;
  pure scoring + integrity + persistence wiring are unit-green locally).
