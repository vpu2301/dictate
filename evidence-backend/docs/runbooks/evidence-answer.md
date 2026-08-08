# Runbook — evidence-answer (:8013)

Quick Search question engine: triage → intent → plan → retrieve → synthesize →
persist, streamed over SSE.

## Dependencies

| Dependency | Degradable? | Effect when down |
|---|---|---|
| Postgres (`app_role`, `audit_writer`, `crypto_writer`) | no | `/readyz` 503 |
| Redis | no | `/readyz` 503 (holds the in-flight cap) |
| evidence-model-gateway (`generator.fast`, `generator.heavy`) | **no** | `/readyz` 503 — without a generator there is no answer |
| evidence-retrieval | **no** | `/readyz` 503 — without evidence there is nothing to answer from |
| evidence-websearch (via retrieval) | **yes** | `web_unavailable` flag, corpus-only answer |
| MinIO / object store | no | answers cannot be persisted (stream still completes, trailing `error` event) |

Only the web is degradable. Reporting ready without a generator would mean
serving `insufficient_basis` to every caller while looking healthy.

## Start locally

`make status` at any point prints which of the ports below are actually live,
and whether each service is merely *listening* or genuinely *ready*. Start
there — an unstarted uvicorn leaves no crash, no log and no container, so
without it the first symptom is a red row in the SPA health panel.

### One command (compose)

```bash
(cd ../medical-dictation-backend && make dev-up && make migrate-up && make seed)
make run-generators   # :8081 + :8082 — SEPARATE terminal, host processes, see below
make evidence-up-all  # infra + all five services, ordered by health
```

`evidence-up-all` waits on health, not on start: the gateway comes up first
(on a cold volume it downloads BGE-M3, ~2.3 GB — minutes, not a hang), then
retrieval, then answer. That ordering is the point — `evidence-answer` asserts
a reachable generator and a reachable retrieval in its own `/readyz`, so
starting it early just produces a container that is up and 503.

### Or service by service (faster edit loop)

```bash
(cd ../medical-dictation-backend && make dev-up && make migrate-up && make seed)
make evidence-up                      # opensearch, clamav, bucket, egress proxy, searxng
make run-generators                   # :8081 + :8082
make run-model-gateway                # :8015
make run-retrieval                    # :8011
make run-websearch                    # :8014
make run-answer                       # :8013
```

Six terminals. `make status` tells you which one you forgot.

### The generators

`generator.fast` (:8081, gemma-3-4b-it) and `generator.heavy` (:8082,
gemma-3-12b-it) are the ADR-0006 dev pins, served by llama-server. Without
them the gateway is honestly unready (rule BE6) and so is this service; for
retrieval-only work run the gateway with `EVA_GATEWAY_GENERATION_ENABLED=false`
instead of faking a generator.

```bash
make run-generators          # foreground; Ctrl-C stops both
```

First run downloads ~10 GB of GGUF from `ggml-org` into
`~/.cache/huggingface/hub` and takes a while; later runs are cache hits and
start in seconds. For an offline or baked-image run, point it at local files
so nothing resolves at runtime (rule BE6):

```bash
EVA_GEN_FAST_GGUF=/opt/models/gemma-3-4b-it-Q4_K_M.gguf \
EVA_GEN_HEAVY_GGUF=/opt/models/gemma-3-12b-it-Q4_K_M.gguf make run-generators
```

Four traps, all of which produce confusing output rather than a clean failure:

- **Ollama blobs do not work.** The platform mounts its `ollama pull gemma3:1b`
  blob straight into a llama-server, so the same trick looks available for 4b
  and 12b. It is not — Ollama writes those with its own converter and
  llama-server rejects them with `key not found in model:
  gemma3.attention.layer_norm_rms_epsilon`. 1b happens to load, which is
  exactly what makes the trap convincing.
- **The platform's llama-server on :8089 is not a substitute.** It serves
  gemma3:1b at 4096 tokens — wrong model for both roles, and a context far too
  small for synthesis over retrieved chunks. It answers, it just answers
  badly.
- **Context length is deliberate, and the two ends must agree.** The
  generators launch at 16384 (fast) / 32768 (heavy), and
  `make run-model-gateway` sets `EVA_GATEWAY_MAX_PROMPT_CHARS=40000` to fit
  under both. Reconciled that way round, an over-long prompt is a clean
  gateway 413; left unreconciled, it is a llama-server error at synthesis that
  reads like a prompt bug. The rig keeps the 120 000 default (vLLM, 128k
  context).
- **They must not swap.** Both models resident is ~11 GiB. On a laptop running
  the platform stack too, use `bash scripts/dev/free_ram_for_generators.sh`
  first — it stops the platform containers evidence does not need. Swapping
  does not fail, it just silently invalidates every latency measurement.

The generators are host processes, not compose services, on purpose: there is
no Metal inside the Docker VM, so containerized inference on a Mac is CPU-only
and would put the first-token NFR out of reach for reasons unrelated to the
pipeline. The gateway reaches them via `host.docker.internal`.

## Reading a stream by hand

```bash
TOKEN=$(...)   # platform access token, see the platform README dev creds
curl -N -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"question":"Empiric therapy for CAP in adults?","mode":"quick_search","locale":"en"}' \
  http://localhost:8013/answers
```

Expected event order: `header` → `summary_segment`* → `detail_segment`* →
`source`* → `late_source`* → `done`. `: ping` comments are heartbeats.

## Symptoms → causes

**Every answer comes back `insufficient_basis`.**
Check the traces for the answer (`answer_traces`, stage + meta):
- `retrieve_corpus` with `passages=0` → the corpus is empty, the snapshot
  filter excludes everything, **or the corpus connector timed out**. Check the
  third one first on a dev machine — it is the likeliest and looks exactly like
  the other two. Call `/retrieve` directly and compare `connector_meta` against
  `passages`:

  ```
  meta: [{"kind":"local_corpus","status":"unavailable","latency_ms":604,"count":0}]
  ```

  `status: unavailable` with a `latency_ms` at or just above
  `EVA_RETRIEVAL_LOCAL_TIMEOUT_MS` (default **600 ms**, a rig target) is the
  connector losing a race it cannot win on CPU — a BGE-M3 query embedding alone
  takes ~560–600 ms there, so it flips between ok and unavailable on identical
  input. Zero evidence means the pipeline correctly deflects, so every question
  returns `insufficient_basis` and the corpus looks empty when it is not.
  `make run-retrieval` and the compose entry both override it to 8 000 ms;
  the rig keeps 600. Confirm with `curl` before touching the ingest side.
- `synthesize` with `retry_reason=dangling_citation` twice → the generator is
  ignoring the output grammar. Check the model pin actually loaded
  (`/readyz` on :8015 reports it) — a base model instead of an instruct model
  produces exactly this.
- `retrieval_unavailable` → evidence-retrieval is down; this service should
  already be reporting unready.

**Answers never include web sources.**
In order of likelihood:
1. `EVA_ANSWER_WEB_ENABLED=false` (check the startup WARNING).
2. The `plan` trace says `web_skipped_fallback_intent` → intent extraction is
   failing, so QS1 is (correctly) keeping the question off the wire. Fix the
   extractor, not the plan.
3. The `retrieve_web` trace says `web_unavailable` → see the
   evidence-websearch runbook.

**429 `pipeline_overloaded`.**
The per-user in-flight cap (2). If it fires for a user with nothing running,
a slot leaked: the key is `eva:answer:inflight:{tenant}:{user}` and it expires
in `EVA_ANSWER_INFLIGHT_TTL_S` (180 s). Deleting the key is safe.

**Stream stalls, then the client disconnects.**
Check for an intermediary buffering SSE. The service sets
`X-Accel-Buffering: no` and heartbeats every 10 s; a proxy that strips both
will hold the response until completion.

**`answer_not_persisted` trailing event.**
The answer streamed but the write failed. The content is in the client's
hands and nowhere else — the user cannot reopen it. Check Postgres and MinIO.
Note the deliberate ordering: if the *audit* write fails, the answer row is
deleted (audit is not best-effort), so an un-auditable answer never exists.

## Triage

Deflection rate by reason is a first-class metric. A jump in
`emergency` deflections is either an incident or a rule regression — check
`make triage-eval` (rules only, no model, reproducible anywhere) before
touching anything else. The gate is 100% emergency recall with 0% false
deflection on the lookalike set; if it is green, the rules did not change.

To see why one question deflected: the decision's `matched_rule` is in the
`triage` stage trace (`note=rule:<id>` or `classifier:<reason>`).

## Prompts and pipeline version

Prompts live in `src/evidence_answer/prompts/` and are recorded per answer in
`answer_provenance.prompt_versions` as `version+sha256[:12]` — so a prompt
edited without a version bump is still traceable. Changing any prompt
requires a `CHANGELOG.md` entry (`make check-prompt-changelog`) **and** a
`pipeline_version` bump, and triggers the standing gate (rule LM3).

## Data

| Table | Written by | Notes |
|---|---|---|
| `questions` | `POST /answers` | user-private RLS (`app.user_id`); may contain incidental PHI |
| `answers` | persist | `envelope_ref` → encrypted object; `verified=false` until S06 |
| `answer_segments` | persist | `ord` = render order, summary then detail |
| `answer_provenance` | persist | **append-only** (trigger); survives answer erasure |
| `answer_traces` | persist | 90-day retention, reaper is S12 work |

Connections to `questions`/`answers` must set BOTH `app.tenant_id` and
`app.user_id` — use `deps.user_connection`, never `tenant_connection`
directly, or the insert is silently rejected by RLS.
