# Medical Dictation — Backend

![CI](https://github.com/your-org/medical-dictation-backend/actions/workflows/ci.yml/badge.svg)
![Coverage](https://codecov.io/gh/your-org/medical-dictation-backend/branch/main/graph/badge.svg)

> **Target onboarding time:** < 30 minutes from `git clone` to a running local stack.

---

## Repository layout

```
medical-dictation-backend/
├── services/               # Independently deployable FastAPI services
│   └── _template/          # Baseline template — copy this to create a new service
├── libs/                   # Internal shared Python packages (uv workspace members)
│   ├── auth/               # JWT verification, Keycloak integration
│   ├── db/                 # Async SQLAlchemy engine factory, base model
│   ├── observability/      # Structured logging, OTel tracing + metrics, PII filter
│   ├── audit/              # HIPAA-compliant audit event recorder
│   └── messaging/          # Kafka producer/consumer abstractions
├── infra/                  # Docker Compose supporting config
│   ├── grafana/            # Dashboard JSON + Grafana provisioning
│   ├── keycloak/           # Realm export (imported on first start)
│   ├── loki/               # Loki config
│   ├── otel/               # OpenTelemetry Collector config
│   ├── postgres/           # DB init SQL (extensions, keycloak DB)
│   └── prometheus/         # Prometheus scrape config
├── scripts/
│   ├── seed/               # seed.sql + seed.py — dev DB seed data
│   ├── doctor.sh           # Environment prerequisites checker
│   └── smoke-test.sh       # Post-startup verification
├── docs/
│   └── adr/                # Architecture Decision Records
├── .github/
│   ├── workflows/ci.yml    # GitHub Actions CI pipeline
│   └── PULL_REQUEST_TEMPLATE.md
├── docker-compose.yml
├── Makefile
└── pyproject.toml          # uv workspace root
```

---

## Prerequisites

| Tool | Minimum | Install |
|------|---------|---------|
| Docker + Compose plugin | 25.0 / 2.20 | https://docs.docker.com/get-docker/ |
| Python | 3.12 | https://python.org |
| uv | 0.4 | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| make | any | OS package manager |
| git | 2.40 | https://git-scm.com |
| ffmpeg | any | OS package manager (`brew install ffmpeg`) — only for the local WER eval (decodes corpus audio) |

> **Windows:** WSL2 is required for acceptable Docker performance. Run `make doctor` to verify.

---

## Quickstart (< 5 minutes)

```bash
git clone https://github.com/your-org/medical-dictation-backend
cd medical-dictation-backend

# 1. Check your environment
make doctor

# 2. Start the full stack (PostgreSQL, Redis, MinIO, Kafka, Keycloak, observability)
make dev-up

# 3. (Optional) seed the dev database
make seed

# 4. Verify everything is healthy
make smoke-test
```

### Service URLs after `make dev-up`

| Service | URL | Credentials |
|---------|-----|-------------|
| PostgreSQL | `localhost:5432` | `postgres/postgres` |
| Redis | `localhost:6379` | — |
| MinIO (API) | `http://localhost:9000` | `minioadmin/minioadmin` |
| MinIO (Console) | `http://localhost:9001` | `minioadmin/minioadmin` |
| Kafka | `localhost:9092` | — |
| Keycloak | `http://localhost:8088` | `admin/admin` |
| Jaeger UI | `http://localhost:16686` | — |
| Prometheus | `http://localhost:9090` | — |
| Grafana | `http://localhost:3000` | `admin/admin` |
| Loki | `http://localhost:3100` | — |

---

## Run the ENTIRE backend (infra + all services) in Docker

`make dev-up` starts **infra only** (the documented dev loop runs services on
the host via `make run-*`). To bring up the **whole backend** — every
application service, plus a one-shot DB migrate and seed — in containers:

```bash
docker compose build      # builds all 8 service images (+ the migrate/seed tools image)
docker compose up         # infra → migrate → seed → keycloak → all services
```

This works because `docker-compose.override.yml` is auto-merged on a plain
`docker compose` invocation. `make dev-up` passes `-f docker-compose.yml`
explicitly, which disables the override — so it stays infra-only, unchanged.

Startup order is enforced with health/`service_completed_successfully` gates:
Postgres (creates roles via `init.sql`) → **migrate** (27 SQL migrations) →
**seed** (dev tenants/users, voice commands, abbreviations, medical prompts,
16 system templates, dev role-logins) → Keycloak (realm import) → services.

| Service | URL | Notes |
|---------|-----|-------|
| auth-service | `http://localhost:8000` | the SPA expects this origin |
| asr-service | `http://localhost:8001` | batch ASR submit/status |
| dictation-service | `http://localhost:8002` | streaming ASR (WebSocket) |
| core-service | `http://localhost:8003` | patients & the per-patient record (encounters, notes, consents, anamnesis, privacy, timeline) |
| nlp-service | `http://localhost:8005` | `/readyz` is 503 in dev — punctuation model disabled by config; `/healthz` is 200 and the pipeline is functional |
| report-service | `http://localhost:8006` | |
| autocomplete-service | `http://localhost:8007` | |
| signing-service | `http://localhost:8008` | КЕП e-signature + public `/verify` |
| asr-worker | — | Redis-stream consumer (no HTTP port) |

> **First build downloads models.** `asr-worker` and `dictation-service` bake
> the pinned `faster-whisper-tiny` weights from Hugging Face at build time
> (offline at runtime), so the first `docker compose build` needs network and
> takes longer. All services run CPU-only here; add `-f infra/compose/gpu.yml`
> for the CUDA overlay.

Health-check every service once up:

```bash
for p in 8000 8001 8003 8005 8006 8007 8008; do
  curl -s -o /dev/null -w "%{http_code}  :$p/healthz\n" http://localhost:$p/healthz
done
```

---

## Running the template service locally

```bash
cd services/_template

# Install deps (creates .venv automatically)
uv pip install -e ".[dev]"

# Copy env template (defaults work with make dev-up)
cp .env.example .env.local

# Start
uv run uvicorn template_service.main:app --reload

# Visit: http://localhost:8000/docs
```

---

## Creating a new service

```bash
cp -r services/_template services/my-service
# Rename package, update pyproject.toml name/description, add to infra as needed
```

---

## Development commands

```bash
make lint          # ruff check
make type-check    # mypy --strict
make test          # pytest
make test-cov      # pytest with coverage report (gate: ≥ 70%)
make security-scan # bandit
make dev-down      # stop & remove containers
make doctor        # environment health check
make wer-eval-corpus # WER release-gate measurement over eval/corpus
```

> **WER eval on macOS:** `make wer-eval-corpus` (or a bare
> `uv run python scripts/eval/run_wer.py --corpus eval/corpus`) works out of
> the box on a dev laptop — no GPU/CUDA needed. On macOS the harness
> auto-selects a CPU-friendly config (`MD_ASR_DEVICE=cpu`,
> `MD_ASR_COMPUTE_TYPE=int8`, `MD_ASR_MODEL=tiny`) and `faster-whisper` is
> pulled into the dev venv via the macOS-gated `dev` dependency-group.
> Requires `ffmpeg` (see Prerequisites). These local numbers are
> **plumbing-only** — the real release gate runs `large-v3` on the Linux/GPU
> rig. See [`docs/eval/wer-methodology.md`](docs/eval/wer-methodology.md).

---

## CI pipeline

Every pull request runs automatically on GitHub Actions:

1. **Lint** — `ruff check` + `ruff format --check`
2. **Type check** — `mypy --strict`
3. **Tests** — `pytest` with coverage gate ≥ 70%
4. **Security** — `bandit` (SAST) + `semgrep` (OWASP Top 10, secrets)
5. **Container scan** — `trivy` (CRITICAL/HIGH CVEs fail the build)
6. **Publish** — builds and pushes to GHCR on merge to `main` with semver tags

---

## Observability

All services emit traces, metrics, and logs to the local OTel Collector:

- **Traces** → Jaeger (`http://localhost:16686`)
- **Metrics** → Prometheus (`http://localhost:9090`) → Grafana
- **Logs** → Loki → Grafana (`http://localhost:3000`)

A pre-built Grafana dashboard for the template service is at
`infra/grafana/dashboards/template-service.json` and auto-provisioned on startup.

### PII safety

`libs/observability` includes a `PIISafeFilter` that redacts the following fields
from all log output before they can be written anywhere:

`patient_*`, `transcript`, `audio_*`, `name`, `email`, `phone`, `ssn`, `address`, `date_of_birth`

---

## Architecture decisions

| # | Decision | Record |
|---|----------|--------|
| 0001 | Python version pin and `uv` workspace | [docs/adr/0001-python-version-and-uv.md](docs/adr/0001-python-version-and-uv.md) |
| 0002 | Distroless, nonroot production containers | [docs/adr/0002-distroless-nonroot-container.md](docs/adr/0002-distroless-nonroot-container.md) |
| 0003 | Typed `Secret[T]` wrapper | [docs/adr/0003-secret-typed-wrapper.md](docs/adr/0003-secret-typed-wrapper.md) |
| 0004 | Single-helper tenant connection (RLS) | [docs/adr/0004-rls-tenant-connection.md](docs/adr/0004-rls-tenant-connection.md) |
| 0005 | Observability stack | [docs/adr/0005-observability-stack.md](docs/adr/0005-observability-stack.md) |

Full index: [docs/adr/README.md](docs/adr/README.md)

---

## Security notes

- **AUTH_BYPASS_DEV=true** disables JWT enforcement for local development only.
  The service logs a `WARNING` on startup when this is set. It **must never** be
  enabled in staging or production.
- Never commit `.env.local`, `*.pem`, or `*.key` files — they are gitignored.
- See `docs/adr/003-secret-management.md` for the full secret management decision.
