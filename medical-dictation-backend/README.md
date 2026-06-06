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
```

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
