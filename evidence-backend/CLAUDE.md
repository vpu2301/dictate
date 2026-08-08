# CLAUDE.md — evidence-backend

Agent rules for the Evidence AI workspace (codename `evidentia`). Read
`~/Desktop/evidence-framework/01-development-rules.md` for the full rule book
(rules are cited by ID: E1–E13, CS, ET, RC, …). This file is the
will-silently-break-you list.

## Placement (ADR-0001)

This is a **separate uv workspace**, sibling to `../medical-dictation-backend`
(the platform). It inherits — never copies — the platform's:

- **Auth/users:** RS256 tokens from platform auth-service :8000 (Keycloak);
  no evidence user store, ever (rule I1).
- **Permissions:** shared matrix in `../medical-dictation-backend/libs/auth/perms.py`
  ↔ `docs/auth/permissions.csv` (bidirectional drift test). Evidence actions are
  `evidence.*` with target kinds `evidence` / `evidence_corpus`.
- **Migrations:** the platform chain `../medical-dictation-backend/infra/postgres/migrations/`
  (0067 = evidence domain). One Postgres, RLS + FORCE everywhere,
  `current_setting('app.tenant_id', true)::uuid` idiom. Run platform
  `make migrate-up` / `check-rls` after touching them.
- **Audit:** `libs/audit.AuditWriter` into the shared hash chain (kinds go in
  the platform's `docs/audit/event-kinds.md`).
- Services consume platform libs as **path dependencies**. Two leaf libs are
  gated by `make check-leaf-imports`: `libs/evidence_models` (stdlib +
  pydantic) and `libs/evidence_safety` (stdlib only — the shared LM4
  injection screen and the QS1 identifier screen; it lives in a lib because
  E2 forbids evidence-websearch importing evidence-ingest).

**A sprint is done only when BOTH workspaces' gates are green** (this `make ci`
AND platform `make ci`/`ci-with-db` when platform files changed).

## Commands

`make help` is authoritative. `make ci` = lint + mypy --strict + leaf gate +
import-linter + QS1 gate + prompt-changelog gate + contracts gate + openapi
gate + tests + security + security-web. DB suite: `make test-integration-db`
(needs platform `dev-up` + `migrate-up`; env gate `RUN_DB_INTEGRATION=1`).

Standing gates runnable on their own: `make qs1-taint` (QS1 layer 3),
`make security-web` (SSRF + egress policy), `make triage-eval`
(AC-S04-B-3), `make eval-retrieval` (rule T4).

## Contracts discipline

- Wire models: Pydantic v2, `extra="forbid"`, StrEnum lowercase snake, final.
- **Any model change ⇒ `make contracts-dump`** or `contracts-check` fails.
- Released schemas are additive-only vs the `evidence-contracts-v*` tag
  (`scripts/contracts/lint_additive.py`); breaking ⇒ new major + new file + ADR.
- ET2 lives in `envelope.py` (validator) AND migration 0067 (CHECK) — change
  both or neither.
- dictat types are generated (`npm run typegen:evidence` in `~/Desktop/dictat`),
  never hand-edited.

## Running the stack

`make status` first, always — it prints which of the platform / infra /
generator / service ports are live AND whether each is merely listening or
actually `/readyz`-ready. The services are plain uvicorn processes, so an
unstarted one leaves no crash, no log and no container; without `status` the
first symptom is a red row in the SPA health panel.

- `make evidence-up-all` — infra + all five services in compose, one command.
- `make run-generators` — `generator.fast` (:8081) / `generator.heavy` (:8082),
  **host processes, not containers** (no Metal inside the Docker VM). Nothing
  answers without them: the gateway's `/readyz` asserts every enabled role.
- `bash scripts/dev/free_ram_for_generators.sh` before a latency run — both
  models resident is ~11 GiB, and swapping does not fail, it just makes the
  measurement wrong.

Traps that produce confusing output rather than a clean failure:

- **Ollama blobs are not llama-server GGUFs for gemma3 4b/12b.** The platform
  mounts its `ollama pull gemma3:1b` blob straight into a llama-server, so the
  trick looks transferable. It is not — 4b/12b are written by Ollama's own
  converter and llama-server rejects them (`key not found in model:
  gemma3.attention.layer_norm_rms_epsilon`). 1b happens to load, which is what
  makes the trap convincing. Weights come from `ggml-org/gemma-3-*-it-GGUF`.
- **The platform's llama-server on :8089 is not a substitute** — gemma3:1b at
  4096 ctx, wrong model for both roles.
- **Generator context and the gateway prompt cap must agree.** Generators run
  at 16384/32768 ctx; `make run-model-gateway` sets
  `EVA_GATEWAY_MAX_PROMPT_CHARS=40000` to fit under both, so an over-long
  prompt is a clean 413 instead of a backend error that reads like a prompt
  bug. The rig keeps the 120 000 default.
- **Compose builds from `~/Desktop/dictate`, not from here.** The platform
  libs are path deps outside this tree; a context rooted at evidence-backend
  cannot see them and `uv pip install` quietly falls back to PyPI for
  `db`/`auth`/`audit`. Every internal dep is listed explicitly in each
  Dockerfile, transitive ones included (`storage → crypto → secret`) —
  anything omitted is not a build error, it is a PyPI fetch.

## Gotchas (S04)

- **The web agent has no direct network path.** Everything leaves through the
  squid egress proxy (ADR-0005); with `EVA_EGRESS_PROXY_URL` unset the HTTP
  client is pointed at a discard port so fetches fail closed. `make
  run-websearch` runs on the host and *cannot* reach the compose proxy — for
  anything that actually fetches, run the service in compose.
- **QS1's import contract needs `allow_indirect_imports = true`** and this is
  not a weakened gate: `evidence_models/__init__` re-exports `snapshot`, so
  every importer picks it up transitively. The symbol-level half is `make
  check-qs1`. Do not "fix" the contract by removing the flag — it becomes
  unsatisfiable.
- **Two allowlists must agree.** A domain in `web_domains` but missing from
  `infra/compose/egress-proxy/allowlist.txt` is citable and unfetchable.
  `make security-web` cross-checks the seeded set.
- **The web connector refuses to search without a `ClinicalIntent`.** That is
  QS1, not a bug: a `PreparedQuery` is text, and from S05 it could be text
  enriched with patient context.
- **Generation roles gate gateway readiness.** With
  `EVA_GATEWAY_GENERATION_ENABLED=true` (the default) and no llama-server,
  `/readyz` is 503 — and so is evidence-answer's. Set it to `false` for
  embed/rerank-only (retrieval-side) work.
- **`questions`/`answers` need BOTH GUCs.** Use `deps.user_connection`
  (`app.tenant_id` + `app.user_id`), never `tenant_connection` directly, or
  the insert is silently rejected by dual-key RLS. The GUC is `app.user_id`;
  the column is `user_sub`.
- **Changing a prompt requires a `prompts/CHANGELOG.md` entry** (CI gate) AND
  a `pipeline_version` bump — the pin is recorded in every answer's
  provenance.
- **`make triage-eval` is a release gate**, not a report: 100% emergency
  recall AND 0% false deflection on the lookalike set. Both directions, or
  the rules are useless in one of the two ways that matter.

## Gotchas (S02)

- The evidence compose file (`infra/compose/evidence.yaml`) joins the
  platform's docker network `medical-dictation_default` as external — the
  platform stack must be up FIRST. `make evidence-up` starts opensearch +
  clamav + the eva-corpus bucket.
- plain `clamav/clamav:<ver>` tags have NO arm64 manifest — use the
  `-debian13-slim` variants on Apple Silicon.
- The gateway's first start downloads BGE-M3 (~2.3 GB) and serves /readyz
  only after the model loads and passes the dimension check.
- OpenSearch has no RLS: every retrieval-side query MUST filter
  `tenant_id ∈ {caller, nil-uuid}` (ADR-0003; enforced in S03).
- `messaging` consumer: the stream message id lives in
  `message.headers["_id"]`, not `.offset`; construct redis clients with
  `decode_responses=False`.
- Platform `S3Client` cannot create buckets — the compose one-shot
  `minio-init-evidence` (mc) owns bucket creation.

## Gotchas

- `uv` may drop from PATH in shell loops — use `/Users/volodymyrpugachov/.local/bin/uv`.
- Per-user GUC is **`app.user_id`** (platform precedent), not `app.user_sub`;
  the *column* is `user_sub`.
- `answer_provenance.answer_id` is deliberately not an FK (survives erasure).
- After any scoped transaction a session's `app.tenant_id` reverts to `''` not
  NULL — unscoped probes need a fresh connection.
- 0067's down migration fails while seeded `knowledge_admin` users exist.
