# dictate

Monorepo for a **medical dictation platform**. The backend lives in
[`medical-dictation-backend/`](medical-dictation-backend/) — a multi-tenant,
HIPAA-conscious FastAPI microservice system for Ukrainian/English clinical
speech-to-text, NLP post-processing, structured reports, and qualified
electronic signing (КЕП / KEP).

> This README is the entry point and the **working context for Claude Code**.
> If you are an AI agent picking up work here, read the whole file first — the
> [Architectural rules](#architectural-rules-do-not-break) and
> [Gotchas](#gotchas--lessons-learned) sections will save you from breaking CI.

---

## What this system does

Clinicians dictate medical reports by voice. The platform:

1. **Records & transcribes** audio (batch + real-time streaming) with Whisper.
2. **Post-processes** the raw transcript (punctuation, numbers, dates,
   abbreviations, voice commands, confidence scoring).
3. **Structures** the text into section-aware clinical report templates.
4. **Versions, diffs, and searches** finalized reports.
5. **Signs** them with a Ukrainian qualified electronic signature (КЕП) and
   exposes public signature verification.
6. **Autocompletes** clinical phrases with sub-80 ms latency.

All PHI (audio + transcripts) is envelope-encrypted at rest and scrubbed from
logs. Multi-tenancy is enforced at the database layer with PostgreSQL
Row-Level Security (RLS), not in application code.

---

## Repository layout

```
dictate/
├── README.md                     # ← you are here (project + Claude Code context)
└── medical-dictation-backend/    # the backend monorepo (uv workspace)
    ├── services/                 # independently deployable FastAPI services
    │   ├── _template/            # baseline — copy this to create a new service
    │   ├── auth-service/         # login/refresh/logout, admin users, audit API
    │   ├── asr-service/          # batch ASR orchestrator (CPU) + validators
    │   ├── asr-worker/           # batch ASR GPU consumer (faster-whisper)
    │   ├── dictation-service/    # real-time streaming ASR over WebSocket
    │   ├── nlp-service/          # 6-stage transcript post-processing pipeline
    │   ├── report-service/       # templates + reports (versioning/diff/search)
    │   ├── signing-service/      # КЕП/KEP signing + public /verify
    │   └── autocomplete-service/ # clinical phrase autocomplete (trie + Redis)
    ├── libs/                     # internal shared packages (workspace members)
    │   ├── secret/               # Secret[T] typed wrapper (leak-proof)
    │   ├── observability/        # logging + OTel traces/metrics + PII filter
    │   ├── db/                   # async SQLAlchemy/asyncpg + RLS tenant helper
    │   ├── auth/                 # JWT verify, JWKS cache, claims, perms matrix
    │   ├── audit/                # hash-chained, append-only audit log
    │   ├── messaging/            # Redis Streams producer/consumer (+ Protocols)
    │   ├── crypto/               # envelope encryption (KEK master→tenant→DEK)
    │   ├── storage/              # EncryptedObjectStore (only sanctioned blob IO)
    │   ├── asr_models/           # shared Pydantic models for ASR
    │   ├── template_models/      # shared Pydantic models for templates (leaf)
    │   └── report_models/        # shared Pydantic models for reports (leaf)
    ├── infra/                    # compose, keycloak realm, prometheus, grafana…
    ├── scripts/                  # db migrate, ci gates, seeds, eval, dev tools
    ├── docs/                     # ADRs, runbooks, architecture, threat model
    ├── eval/                     # WER evaluation corpus
    ├── tests/                    # cross-service integration tests
    ├── docker-compose.yml        # (+ infra/compose/{base,dev,gpu}.yml split)
    ├── Makefile                  # all dev/CI commands — run `make help`
    └── pyproject.toml            # uv workspace root + tool config + contracts
```

---

## Tech stack

| Concern            | Choice                                                        |
|--------------------|--------------------------------------------------------------|
| Language / runtime | Python **3.12** (pinned to `3.12.7` in `.python-version`)     |
| Package manager    | **uv** workspace (root `pyproject.toml`, `uv.lock` committed) |
| Web framework      | FastAPI (+ WebSockets for streaming dictation)               |
| DB                 | PostgreSQL with **RLS + FORCE** on every user-schema table   |
| Cache / queue      | Redis (Redis **Streams** for jobs; Kafka reserved for later) |
| Object storage     | MinIO / S3 (always via `libs/storage`, always encrypted)     |
| Identity           | **Keycloak** realm `medical-dictation` (RS256 JWT, OIDC)     |
| ASR                | faster-whisper + Silero VAD (GPU; CPU `tiny/int8` fallback)  |
| Observability      | OpenTelemetry → Jaeger / Prometheus / Loki / Grafana         |
| Signing            | КЕП/KEP via Дія + ІІТ providers (mock for dev), PAdES-LTV    |
| Lint / type / sec  | ruff, mypy `--strict`, bandit, semgrep, import-linter, trivy |

---

## Quickstart

```bash
cd medical-dictation-backend

make doctor        # check prerequisites (Docker, Python 3.12, uv, make, git)
make dev-up        # start the full local stack
make migrate-up    # apply SQL migrations
make seed          # seed dev tenants + sample data
make smoke         # verify the stack is healthy
```

Prerequisites: Docker + Compose plugin (25.0 / 2.20+), Python 3.12, uv 0.4+,
make, git. On Windows use WSL2. `make doctor` validates all of this.

### Laptop / GPU variants

```bash
make dev-up-asr    # base + dev overlay, CPU-only Whisper (no GPU needed)
make dev-up-gpu    # base + dev + GPU overlay (requires NVIDIA toolkit)
```

### Local service URLs (after `make dev-up`)

| Service          | URL                       | Credentials             |
|------------------|---------------------------|-------------------------|
| PostgreSQL       | `localhost:5432`          | `postgres/postgres`     |
| Redis            | `localhost:6379`          | —                       |
| MinIO API/Console| `:9000` / `:9001`         | `minioadmin/minioadmin` |
| Kafka            | `localhost:9092`          | —                       |
| Keycloak         | `http://localhost:8088`   | `admin/admin`           |
| Jaeger           | `http://localhost:16686`  | —                       |
| Prometheus       | `http://localhost:9090`   | —                       |
| Grafana          | `http://localhost:3000`   | `admin/admin`           |

---

## Common commands (run from `medical-dictation-backend/`)

```bash
# Quality
make lint            # ruff check
make lint-fix        # ruff --fix
make typecheck       # mypy --strict (all packages)
make test            # pytest across services and libs
make test-cov        # pytest + coverage (gate: ≥ 80%)
make security        # bandit + pip-audit + semgrep
make lint-imports    # import-linter — architectural contracts

# CI mirrors (run these before pushing)
make ci              # lint + typecheck + test + security + the grep gates
make ci-with-db      # full CI incl. RLS check + OpenAPI drift (needs dev-up + migrate-up)

# Database
make migrate-up      # apply pending SQL migrations
make migrate-down    # roll back the most recent migration
make migrate-status  # show applied + pending
make reset-db        # wipe & recreate the dev Postgres volume

# Seeds
make seed            # core dev tenants + sample data
make seed-prompts    # medical_prompts (uk/en × specialties)
make seed-voice-commands
make seed-abbreviations
make seed-templates  # 16 system report templates

# Auth / identity
make keycloak-test   # login → introspect → refresh → replay-rejected smoke
make keycloak-export # re-extract realm JSON from running container

# Evaluation
make wer-eval            # batch WER harness
make wer-eval-streaming  # streaming WER harness
make wer-eval-per-section

make help            # full target list with descriptions
```

CI-only grep/policy gates (also part of `make ci`):
`check-rls`, `check-audit-insert`, `check-no-object-storage`, `check-no-crypto`,
`validate-templates`, `openapi-check`.

---

## Architectural rules (DO NOT break)

These are enforced by **import-linter contracts** (`pyproject.toml`),
**CI grep gates**, and **pre-commit hooks**. Violating them fails CI.

1. **Layering inside services:** imports flow `routers → domain → adapters`
   (and the per-service variants for dictation/nlp). The reverse is forbidden.
2. **Libs may never import services.**
3. **Leaf libs:** `secret`, `observability`, `template_models`, `report_models`,
   `asr_models` must not import other internal libs. `crypto` must not import
   `storage`/`messaging`; `storage` layers on `crypto` only.
4. **All PHI blob IO goes through `libs/storage.EncryptedObjectStore`.** There is
   no plaintext bypass. Direct `boto3`/`aioboto3`/`minio` outside `libs/storage`
   fails `check-no-object-storage`.
5. **All cryptography goes through `libs/crypto`.** Direct `cryptography.hazmat`
   outside `libs/crypto` fails `check-no-crypto`.
6. **All audit writes go through `libs/audit.AuditWriter`.** Direct inserts into
   `audit.events` fail `check-audit-insert`. The chain is hash-linked and
   append-only; the DB has an immutability trigger.
7. **Every user-schema table has RLS + FORCE enabled** (often plus a RESTRICTIVE
   policy for defence-in-depth). `check-rls` queries `pg_class` to verify.
   Tenant scoping uses `libs/db.tenant_connection(pool, tenant_id)` — the single
   sanctioned RLS-scoped connection helper (`SELECT set_config('app.tenant_id',
   …, true)`, transaction-local).
8. **No raw `asyncpg.connect`/`create_pool` outside `libs/db/`** (pre-commit).
9. **No `os.environ`/`os.getenv` outside `config.py`** (pre-commit) — config is
   centralized and typed.
10. **Sensitive values use `libs/secret.Secret[T]`** — it blocks repr/str/format/
    JSON/pickle/copy leak channels.
11. **Wire/API models use Pydantic `extra="forbid"`** (strict). Streaming and
    replay-determinism gates depend on this.

When you genuinely need to relax a contract, that requires an **ADR amendment**
in `docs/adr/`, not a quiet edit.

---

## How to do common tasks

- **Add a new service:** `cp -r services/_template services/my-service`, rename
  the package, update `pyproject.toml` name + `tool.uv.sources`, add it to the
  import-linter `root_packages` and (if needed) a layering contract, and wire
  infra/compose entries.
- **Add a new lib:** same copy pattern; register in import-linter `root_packages`
  and add a `forbidden` contract if it must stay a leaf.
- **Add a migration:** write `NNNN_name.sql` + `NNNN_name.down.sql` under
  `infra/postgres/migrations/`; new user tables must enable RLS + FORCE. Apply
  with `make migrate-up`; the runner checksums migrations to detect drift.
- **Add an audit event kind:** add it to the service's `audit_kinds.py`, document
  it in `docs/audit/event-kinds.md`, and write via `AuditWriter`.
- **Add a permission:** edit the `ALLOW` matrix in `libs/auth/perms.py` and keep
  `docs/auth/permissions.csv` in sync (a test detects drift in either direction).
- **Add a queue with at-least-once semantics:** use
  `libs/messaging.RedisStreamsProducer`/`Consumer` (XAUTOCLAIM reclaim, DLQ after
  3 retries).
- **Touch a public API:** refresh the OpenAPI snapshot (`make openapi-dump`) or
  `openapi-check` will fail.

---

## Service summaries (built across sprints 01–10)

| Service / area        | What it adds | Key notes |
|-----------------------|--------------|-----------|
| **Foundations** (S01) | `_template`, secret, db, observability, messaging Protocols, import-linter, pre-commit | Health = `/healthz` + `/readyz`; RFC 9457 problem details; coverage gate 80% |
| **auth-service** (S02) | Login/refresh/logout, admin users, audit API; Keycloak realm; RLS; hash-chained audit; perms matrix; MFA stub | **MFA intentionally disabled** in pilot — flip `MDX_REQUIRE_MFA=true` to enforce |
| **asr-service / asr-worker** (S03) | Batch Whisper ASR; 8-step validators; envelope crypto; EncryptedObjectStore; Redis Streams | First PHI-bearing path. AAD = `tenant_id ‖ row_id`; `master.key` mode ≤ 0400 |
| **dictation-service** (S04) | Real-time streaming ASR over WebSocket, protocol `medical-dictation.v1` | Sliding-window Whisper, reconnect/resume, tmpfs ring buffer, binary Opus frames |
| **nlp-service** (S05) | 6-stage pipeline: voice commands → punctuation → numbers → dates → abbreviations → confidence | Ordered contract; `PIPELINE_VERSION` in idempotence cache key; abbrev snapshot per request |
| **report-service** (S06, S08) | Section-aware templates (16 system templates) + reports (versioning, diff, FTS) | Cosmetic-vs-structural edit rule; append-only `report_versions`; linear amendment chain |
| **signing-service** (S09) | КЕП/KEP signing (Дія + ІІТ + mock) + public `/verify` | PAdES-LTV with embedded canonical JSON (JCS); IP-HMAC audit + rate limiter on /verify |
| **autocomplete-service** (S10) | Clinical phrase autocomplete | Trie + Redis cache (per-key lock, version_tag); Bayesian ranking; p95 ≤ 80 ms; PII scrubber |

Detailed per-sprint context lives in the project memory index
(`~/.claude/projects/.../memory/MEMORY.md`) and in `docs/adr/` (ADRs 0001–0025).

---

## Gotchas / lessons learned

- **System Python is PEP 668-locked** — never `pip install` globally. All work
  runs through `uv` in the managed `.venv`.
- **`AUTH_BYPASS_DEV=true`** disables JWT enforcement; **dev only**, logs a
  WARNING on startup, must never reach staging/prod.
- **FastAPI vs Starlette `HTTPException`** are distinct classes; the global
  exception handlers register both.
- **CPU Whisper fallback** loads `tiny`/`int8` for GPU-less laptops; production
  asserts CUDA in the readiness probe.
- **Pre-signed URLs serve ciphertext** — call `EncryptedObjectStore.get()` for
  plaintext.
- **Audit chain on empty tables:** first-write seq collisions are prevented with
  a per-tenant `pg_advisory_xact_lock` (READ COMMITTED, not SERIALIZABLE).
- **Never commit** `.env.local`, `*.pem`, `*.key`, or the dev master key under
  `infra/dev/` — all gitignored.

---

## Documentation map

- `medical-dictation-backend/README.md` — backend-local quickstart & details.
- `docs/testing/README.md` — terminal walkthrough: commands + expected responses to test the system end to end (plus the `mdx-test` CLI).
- `docs/adr/` — Architecture Decision Records (0001–0025); `docs/adr/README.md` index.
- `docs/architecture/` — service topology (e.g. `asr.md`).
- `docs/runbooks/` — operational runbooks (auth, asr-worker, …).
- `docs/security/threat-model.md`, `SECURITY.md` — security posture.
- `docs/auth/` — `roles.md`, `permissions.csv`.
- `docs/audit/event-kinds.md` — audit event catalogue.
- `CONTRIBUTING.md`, `CODEOWNERS`, `LICENSE` (Apache-2.0).

---

## License

Apache-2.0 — see [`medical-dictation-backend/LICENSE`](medical-dictation-backend/LICENSE).
