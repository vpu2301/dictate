# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Where the work happens

The repo root is a thin wrapper. **All backend work happens in `medical-dictation-backend/`** — that is the uv workspace root and where `Makefile`, `pyproject.toml`, and `uv.lock` live. `cd medical-dictation-backend` before running anything below. The frontend SPA is a **separate repo** at `~/Desktop/dictat` (not in this tree).

The root `README.md` is the canonical project brief and is kept current — read it for service summaries and the full command list. This file captures the things that will silently break CI or waste your time if you don't already know them.

## Environment & tooling

- **Python is `uv`-only.** System Python is PEP 668-locked; never `pip install` globally. Everything runs through `uv` against the managed `.venv`.
- **`mypy`, `bandit`, `semgrep` are NOT installed in the venv** — invoke them via `uv run --with "mypy>=1.10" …` (the Makefile targets already do this).
- In shell `for`-loops the PATH can drop `uv`; use the absolute path `/Users/volodymyrpugachov/.local/bin/uv` if `uv` isn't found.
- Docker Desktop must be running (`open -a Docker`) before `make dev-up`.

## Commands

```bash
make help            # full, authoritative target list
make dev-up          # start the infra stack (Postgres, Redis, MinIO, Kafka, Keycloak, OTel/Jaeger/Prom/Grafana/Loki)
make migrate-up      # apply SQL migrations  (migrate-down / migrate-status also exist)
make seed            # seed dev tenants + users
make smoke           # verify the stack

make lint            # ruff check          (lint-fix to autofix)
make typecheck       # mypy --strict over CI-GATED foundation packages only
make typecheck-all   # mypy --strict over ALL packages — NON-BLOCKING (tracks known feature-service typing debt)
make test            # pytest across every package
make security        # bandit + pip-audit + semgrep
make lint-imports    # import-linter — architectural contracts (see below)

make ci              # mirror the blocking CI gates locally (lint + typecheck + test + security + grep gates)
make ci-with-db      # full mirror incl. check-rls + openapi-check (needs dev-up + migrate-up first)
```

**Services are not in `docker-compose` — only infra is.** Run a service locally with uvicorn, e.g. the SPA expects auth-service on :8000: `make run-auth-service` (needs `dev-up && migrate-up && seed`).

### Running tests

`make test` **runs each package separately on purpose** — pytest's pluggy registers conftests by module name, and two `tests.conftest` files in one invocation collide. Follow the same pattern for a single package or test:

```bash
uv run --project libs/crypto pytest libs/crypto/tests/unit/ -v
uv run --project services/nlp-service pytest services/nlp-service/tests/unit/test_x.py::test_y -v
```

Integration / chaos / load tests are env-gated (off by default): `RUN_DB_INTEGRATION=1`, `RUN_KEYCLOAK_INTEGRATION=1`, `RUN_DICTATION_CHAOS=1`, `RUN_DICTATION_LOAD=1` (see `make test-integration-db`, `chaos-dictation`, `load-dictation`).

## Architecture in one paragraph

Multi-tenant, HIPAA-conscious FastAPI microservices for Ukrainian/English clinical speech-to-text. Audio → **asr-service/asr-worker** (batch Whisper) or **dictation-service** (real-time streaming ASR over WebSocket, protocol `medical-dictation.v1`) → **nlp-service** (6-stage post-processing: voice commands → punctuation → numbers → dates → abbreviations → confidence) → **report-service** (section-aware JSONB templates + append-only versioned reports with diff/FTS) → **signing-service** (Ukrainian qualified e-signature КЕП/KEP + public `/verify`). **auth-service** fronts Keycloak; **autocomplete-service** serves clinical phrases. Shared concerns live in `libs/*` (workspace members): `secret`, `observability`, `db`, `auth`, `audit`, `messaging`, `crypto`, `storage`, and leaf `*_models` packages. Tenancy is enforced in the **database** via Postgres RLS, not in app code. All PHI (audio + transcripts) is envelope-encrypted at rest and scrubbed from logs.

## Architectural rules — DO NOT break (enforced; violating fails CI)

These are enforced by import-linter contracts (`pyproject.toml`), CI grep gates, and pre-commit hooks. Relaxing one requires an **ADR amendment** in `docs/adr/`, not a quiet edit.

1. **Layering inside a service:** imports flow `routers → domain → adapters` (per-service variants for dictation/nlp). Reverse is forbidden.
2. **Libs never import services.** Leaf libs (`secret`, `observability`, `*_models`) import no other internal lib; `crypto` doesn't import `storage`/`messaging`; `storage` layers on `crypto` only.
3. **All PHI blob IO goes through `libs/storage.EncryptedObjectStore`.** Direct `boto3`/`aioboto3`/`minio` elsewhere fails `check-no-object-storage`. Pre-signed URLs serve **ciphertext** — use `.get()` for plaintext.
4. **All cryptography goes through `libs/crypto`.** Direct `cryptography.hazmat` elsewhere fails `check-no-crypto`. Envelope is `KEK_master → tenant KEK → DEK`; AAD = `tenant_id ‖ row_id`.
5. **All audit writes go through `libs/audit.AuditWriter`.** Direct inserts into `audit.events` fail `check-audit-insert`. The chain is hash-linked, append-only, with a DB immutability trigger.
6. **Every user-schema table has RLS + FORCE** (often plus a RESTRICTIVE policy). `check-rls` verifies via `pg_class`. Scope queries with `libs/db.tenant_connection(pool, tenant_id)` — the only sanctioned RLS-scoped connection helper (transaction-local `set_config('app.tenant_id', …, true)`).
7. **No raw `asyncpg.connect`/`create_pool` outside `libs/db/`** (pre-commit).
8. **No `os.environ`/`os.getenv` outside `config.py`** (pre-commit) — config is centralized and typed.
9. **Sensitive values use `libs/secret.Secret[T]`** — blocks repr/str/format/JSON/pickle/copy leak channels.
10. **Wire/API models use Pydantic `extra="forbid"`** — streaming and replay-determinism gates depend on it.
11. **Touching a public API requires `make openapi-dump`** or `openapi-check` fails on snapshot drift.

## Gotchas

- **`AUTH_BYPASS_DEV=true`** disables JWT enforcement — dev only, logs a WARNING on startup, must never reach staging/prod.
- **MFA is intentionally disabled** in the pilot — flip `MDX_REQUIRE_MFA=true` to enforce.
- **Keycloak shares the Postgres server** (db `keycloak`). Wiping the pg volume orphans it; use `make reset-db` (force-recreates Keycloak) rather than dropping the volume manually.
- **ASR offline models:** never set `MD_ASR_MODEL` to a bare HF id (`large-v3`/`tiny`) in the offline images — it triggers a runtime HF resolve that fails under `HF_HUB_OFFLINE=1`. Point it at the baked dir (`/opt/models/...`). Model pins: `docs/models/PINS.md`. CPU fallback loads `tiny`/`int8`; production asserts CUDA in the readiness probe.
- **New service Dockerfile:** copying the editable-libs `--target /deps` pattern requires the `sitecustomize.py` doing `site.addsitedir("/deps")` before `ENV PYTHONPATH=/deps` — otherwise `.pth`-installed workspace libs (`asr_models`, etc.) aren't activated and the service dies with `ModuleNotFoundError`. (`uv pip install --no-dev` is also invalid in uv 0.4.29 — don't add it.)
- **FastAPI vs Starlette `HTTPException`** are distinct classes; global handlers register both.

## Adding things

- **New service/lib:** `cp -r services/_template …`, rename the package, update its `pyproject.toml` + `tool.uv.sources`, register in import-linter `root_packages`, add a layering/leaf contract, wire infra/compose.
- **Migration:** `NNNN_name.sql` + `NNNN_name.down.sql` under `infra/postgres/migrations/`; new user tables MUST enable RLS + FORCE. The runner checksums migrations to detect drift.
- **Audit event kind:** add to the service's `audit_kinds.py`, document in `docs/audit/event-kinds.md`, write via `AuditWriter`.
- **Permission:** edit the `ALLOW` matrix in `libs/auth/perms.py` and keep `docs/auth/permissions.csv` in sync (a test detects drift either direction).

## Dev credentials (local only)

All dev users password `dev-password`. Tenant-A admin `admin@tenant-a.example`, tenant-B admin `admin@tenant-b.example`, clinician `clinician@tenant-a.example`. Infra creds in the root `README.md` service-URL table. Never commit `.env.local`, `*.pem`, `*.key`, or the dev master key under `infra/dev/` (all gitignored).

## Decision history

`docs/adr/` holds ADRs 0001–0025 (`docs/adr/README.md` is the index) — the authoritative record for why the load-bearing choices were made. Sprint 00 (the ground-floor foundation: uv workspace, `libs/{secret,db,observability}`, the `services/_template` paved road, distroless/nonroot containers, dev stack, CI gate; ADRs 0001–0005) has its canonical spec reconstructed as-built at `docs/sprints/sprint-00.md`, with sign-off (`docs/signoffs/sprint-00.md`) and retro (`docs/retros/sprint-00.md`). Per-sprint context (00–10, plus verification batches A/B) lives in the project memory index.
