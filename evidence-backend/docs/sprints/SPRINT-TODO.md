# SPRINT-TODO (rolling)

## New from S04

- [x] ~~**Live end-to-end run** (as-built delta 9)~~ — done 2026-08-07, record
      in `docs/sprints/s04-live-run.md`. Real llama-server, real SearXNG, real
      allowlisted fetch through the egress proxy; `status: ok` with citations
      and the documented SSE order. Found and fixed two latent defects (the
      600 ms `local_connector_timeout_ms` starving retrieval on a dev CPU →
      every answer `insufficient_basis`; and evidence-answer being unbuildable
      as a wheel). `make measure-latency` reproduces it.
- [ ] **Latency NFR still NOT met and still not gated.** Measured 11.9–13.8 s
      warm vs a 3.5 s budget, on a laptop, with `generator.heavy` OFF-PIN
      (4b standing in for 12b, which does not fit in 24 GiB). No single stage
      dominates — ~4.5 s of `generator.fast`, ~5.9 s CPU embedding, ~5.6 s
      synthesis — so this is a GPU-rig number, not a tuning exercise.
      **Re-measure on the rig with the real pins; that run is the gate.**
- [ ] ADR-0006 amendment note: the `gemma-3-12b-it` dev pin is not runnable on
      a 24 GiB developer machine alongside the stack (measured: ~11.7 GiB of
      generators + ~6 GiB of stack vs ~14 GiB available, and overshooting
      OOM-kills Docker containers rather than failing cleanly). Either the ADR
      names a smaller dev pin or it states the machine floor.
- [ ] `retrieve_web` returns 0 passages in 0 ms inside the answer pipeline
      while the same intent against `/web/search` directly does reach the
      network — the late-web join needs a look.
- [ ] Web trust-tier coverage: `jamanetwork.com` and
      `uspreventiveservicestaskforce.org` came back `not_allowlisted` on a
      routine statins question. Fold into the shipped-allowlist countersign.
- [ ] **Cycle migration 0070** on a live DB (`make migrate-up` ×2 with down,
      `check-rls`, `check-erasure-fanout`) and run the DB-gated proofs that
      need it: reopen-from-snapshot with fetcher-call-count = 0
      (AC-S04-B-6), dual-key RLS isolation on `questions`/`answers`,
      tenant-shadowing of a shipped `web_domains` row.
- [ ] **Clinical countersign** now also covers: triage set v1 + safe-messaging
      text, synthesis prompt v1, the web trust-tier table + 26-domain shipped
      allowlist, and generator selection (ADR-0006) after the rig run.
- [ ] Grafana **"Evidence / Quick Search"** board: stage latency histograms,
      deflection rate by reason, web fetch outcomes (ok/robots_skip/paywall/
      garbage/error), egress-deny alarm, ephemeral-index size, tokens per
      answer by role. Counters exist in code only.
- [ ] Grow the triage set with real clinician questions; re-run
      `make triage-eval` as a standing gate (nightly, with the CI pipeline).
- [ ] Quick-answer **eval seed** for the S06 gates (citation faithfulness,
      injection probes on fetched web text) — the triage half landed, the
      answer-quality half needs a rig run to be meaningful.
- [ ] `check-no-llm-in-checks` (E13): target dir
      `services/evidence-answer/checks/` does not exist until the
      deterministic safety layer lands (S11).
- [ ] Harness B-2…B-6 (ask stream, deflect, reopen, domain admin, degraded)
      into the deferred batch harness.
- [x] ~~Compose entries for `evidence-answer` / `evidence-websearch` /
      `evidence-retrieval` exist but their **Dockerfiles do not**~~ — all five
      written (2026-08-07), plus an `evidence-ingest-worker` entry, health-
      ordered `depends_on`, `make evidence-up-all` / `evidence-build` /
      `evidence-logs` / `status`. **The compose build context had to move from
      `../..` to `../../..`**: evidence takes the platform libs as path deps
      outside its own tree, so a context rooted at evidence-backend cannot see
      them and `uv pip install` silently resolves `db`/`auth`/`audit` from
      PyPI — an image that builds clean and imports the wrong package.
- [ ] SearXNG `secret_key` comes from a dev-only literal in
      `infra/compose/searxng/settings.yml`; deployment must inject it (S3).

## New from S03

- [ ] **Clinical countersigns** now cover: S01 enums (ADR-0002), S03 scoring
      constants + lexicon v1 + eval rubric (ADR-0004) — one review session
      closes all three sign-off blocks.
- [ ] **Rebaseline after the real bulk load**: rerun ablation + `--adopt` on
      the ≥100k corpus; current baseline is plumbing-grade (27 chunks).
- [ ] CI pipeline for evidence-backend (mirrors `make ci`) + nightly
      `make eval-retrieval` schedule + `eval/gates.yaml` protected-path rule.
- [ ] Grafana "Evidence / Retrieval" board (retrieve_latency by stage,
      rerank_degraded_total, cache hit ratio, score-distribution drift canary)
      — metrics currently counters-in-code only.
- [ ] **Ukrainian lexical quality**: uk BM25 recall .43 vs .80 en (no uk
      stemming in medical_icu) — evaluate a Ukrainian analyzer plugin or
      hunspell dictionary for the OpenSearch index; dense currently carries uk.
- [ ] License review of dictate NLP abbreviation tables → merge into lexicon
      v2 (bump lexicon_version!).
- [ ] `language` column on documents (reindex currently re-derives by
      heuristic).
- [ ] Compose entry + Dockerfile for evidence-retrieval (join the S02 list).

## New from S02

- [ ] **Operator bulk load (AC-S02-B-1)**: run Phase 1 of
      `~/Desktop/evidence-corpus-integration-plan.md` on the rig (or an
      overnight Mac run) → ≥100k real chunks → `snapshot global-v1` →
      chunking review checklist with the clinical advisor (sign-off row open).
- [ ] Wire the Retraction Watch CC0 CSV as a scheduled retractions run.
- [ ] Grafana "Evidence / Ingestion" board + alerts (dead-letter growth,
      backlog stall, quarantine >72h) — needs the S00 observability wiring.
- [ ] Harness checks A-8…A-11 (job lifecycle, snapshot, retraction,
      quarantine) into `batch-a-verify.mjs`.
- [ ] On-prem image bake list: opensearch+analysis-icu, clamav arm64/amd64,
      BGE-M3 weights (BE6) — goes into the S13/S15 deployment work.
- [x] ~~Dockerfiles for evidence-model-gateway + evidence-ingest~~ — written
      2026-08-07 with the other three. The gateway's differs from the rest:
      torch comes from the CPU-only index (the default wheel bundles the CUDA
      runtime) and BGE-M3 weights are NOT baked, so a cold container looks
      hung for minutes while it downloads — hence the 300 s `start_period` and
      the `gateway_models` volume. Baking them is still the on-prem item
      below.

## Blocking S02+ (from S01)

- [ ] **Tag `evidence-contracts-v1.0.0`** on the S01 merge commit (activates the
      additive linter's real baseline) — after clinical countersign of ADR-0002.
- [ ] **Clinical advisor countersign** of the enum set (ADR-0002 / AC-S01-B-6).
- [ ] **S00 leftovers** (never built; owed to Batch A):
  - [ ] `services/_template` evidence variant consuming platform `libs/auth`/`libs/audit` via path deps
  - [ ] `evidence-model-gateway` v0 (self-hosted vLLM, role registry, PINS.md)
  - [ ] evidence docker-compose (separate startup, attaches to platform network)
  - [ ] pre-commit suite for evidence-backend (ruff, gitleaks, detect-private-key, commitizen, AST gates)
  - [ ] dictat evidence module skeleton behind `VITE_FEAT_EVIDENCE` + nav group
  - [ ] `scripts/integration/batch-a-verify.mjs` evidence checks (A-1…A-7)
- [ ] Aggregate CI entry that runs BOTH workspaces' gates (retro R3).

## Non-blocking

- [ ] Platform `check-erasure-fanout`: register evidence tables in the fan-out
      map when S05 links them to patients (today they are user-keyed only).
- [ ] `answer_traces` 90-day reaper job (S12).
- [ ] Wire the CI artifact `evidence-contracts-{gitsha}.tar.gz` once a CI
      pipeline exists for this workspace.
