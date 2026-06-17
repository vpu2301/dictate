.PHONY: dev-up dev-down dev-restart dev-logs smoke smoke-test lint typecheck typecheck-all type-check test test-cov security security-scan ci doctor reset-db help pre-commit-install lint-imports check-no-os-environ check-no-direct-asyncpg dev-up-asr dev-up-gpu seed-prompts check-no-object-storage check-no-crypto wer-eval

COMPOSE = docker compose
COMPOSE_FILE = docker-compose.yml

##@ Local Development

dev-up: ## Start the full local stack (PostgreSQL, Redis, MinIO, Kafka, Keycloak, observability)
	$(COMPOSE) -f $(COMPOSE_FILE) up -d --wait
	@echo ""
	@echo "Stack is up. Service URLs:"
	@echo "  PostgreSQL   : localhost:5432"
	@echo "  Redis        : localhost:6379"
	@echo "  MinIO        : http://localhost:9000 (console: http://localhost:9001)"
	@echo "  Kafka        : localhost:9092"
	@echo "  Keycloak     : http://localhost:8088"
	@echo "  Jaeger UI    : http://localhost:16686"
	@echo "  Prometheus   : http://localhost:9090"
	@echo "  Grafana      : http://localhost:3000  (admin/admin)"
	@echo "  Loki         : http://localhost:3100"

dev-down: ## Stop and remove all containers
	$(COMPOSE) -f $(COMPOSE_FILE) down -v

dev-restart: dev-down dev-up ## Full restart of the local stack

dev-logs: ## Tail logs from all containers
	$(COMPOSE) -f $(COMPOSE_FILE) logs -f

smoke: ## Run smoke tests against the local stack (alias of smoke-test)
	@bash scripts/smoke-test.sh

smoke-test: smoke ## Legacy alias of `smoke`

keycloak-test: ## Smoke-test Keycloak: login → introspect → refresh → replay-rejected
	@bash scripts/dev/keycloak-test.sh

keycloak-export: ## Re-extract realm JSON from the running Keycloak container into infra/keycloak/
	@bash scripts/dev/keycloak-export.sh

seed: ## Seed the dev database with sample data
	uv run python scripts/seed/seed.py

.PHONY: run-auth-service
run-auth-service: ## Run auth-service on :8000 for the SPA (needs dev-up + migrate-up + seed)
	# The SPA (VITE_AUTH_SERVICE_URL) expects auth-service at http://localhost:8000.
	# CORS allows the Vite dev origin (CORS_ALLOWED_ORIGINS); cookie is SameSite=lax.
	OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 \
	uv run --project services/auth-service uvicorn auth_service.main:app --host 0.0.0.0 --port 8000

doctor: ## Diagnose local environment issues
	@bash scripts/doctor.sh

##@ Code Quality

lint: ## Run ruff linter across all packages
	uv run ruff check .

lint-fix: ## Run ruff with auto-fix
	uv run ruff check --fix .

typecheck: ## Run mypy --strict over the CI-gated foundation packages (mirrors .github/workflows/ci.yml)
	uv run --with "mypy>=1.10" mypy --strict services/_template/src/ libs/observability/src/ libs/secret/src/ libs/auth/src/ libs/db/src/ libs/audit/src/ libs/messaging/src/

typecheck-all: ## Run mypy --strict over ALL packages (non-blocking; tracks feature-service typing debt — see Sprint A1 report)
	uv run --with "mypy>=1.10" mypy --strict services/_template/src/ services/asr-service/src/ services/asr-worker/src/ services/dictation-service/src/ services/nlp-service/src/ services/report-service/src/ libs/observability/src/ libs/secret/src/ libs/auth/src/ libs/db/src/ libs/audit/src/ libs/messaging/src/ libs/crypto/src/ libs/storage/src/ libs/asr_models/src/ libs/template_models/src/

type-check: typecheck ## Legacy alias of `typecheck`

test: ## Run pytest across services and libs
	# Each package has its own pyproject [tool.pytest.ini_options] (testpaths,
	# asyncio_mode). Several have a tests/__init__.py + tests/conftest.py.
	# pytest's pluggy registers conftests by module name; two `tests.conftest`
	# files in one invocation collide, so run each package separately.
	uv run --project services/_template pytest services/_template/tests/ -v
	uv run --project libs/secret pytest libs/secret/tests/ -v
	uv run --project libs/observability pytest libs/observability/tests/ -v
	uv run --project libs/db pytest libs/db/tests/unit/ -v
	uv run --project libs/messaging pytest libs/messaging/tests/ -v
	uv run --project libs/auth pytest libs/auth/tests/ -v
	uv run --project libs/audit pytest libs/audit/tests/unit/ -v
	uv run --project services/auth-service pytest services/auth-service/tests/ -v
	uv run --project libs/crypto pytest libs/crypto/tests/unit/ -v
	uv run --project libs/storage pytest libs/storage/tests/unit/ -v
	uv run --project services/asr-service pytest services/asr-service/tests/unit/ -v
	uv run --project services/dictation-service pytest services/dictation-service/tests/unit/ -v
	uv run --project services/nlp-service pytest services/nlp-service/tests/unit/ -v
	uv run --project libs/template_models pytest libs/template_models/tests/unit/ -v

test-cov: ## Run pytest with coverage report (gate ≥ 80%)
	uv run pytest services/_template/tests/ \
		--cov=template_service \
		--cov-report=term-missing \
		--cov-report=xml \
		--cov-fail-under=80

security: ## Run bandit (SAST) + pip-audit + semgrep
	# bandit: applies [tool.bandit] config (excludes tests, skips B101) and the
	# same confidence filter as CI. MEDIUM is informational; HIGH is blocking.
	uv run --with "bandit[toml]>=1.7.8" bandit -c pyproject.toml -r services/ libs/ --severity-level medium --confidence-level medium --exit-zero
	uv run --with "bandit[toml]>=1.7.8" bandit -c pyproject.toml -r services/ libs/ --severity-level high --confidence-level medium
	uv run --with pip-audit pip-audit || true
	uv run --with semgrep semgrep --config p/owasp-top-ten --error || true

security-scan: security ## Legacy alias of `security`

lint-imports: ## Verify architectural contracts (import-linter)
	# import-linter is not a venv dependency — pull it in for the run.
	uv run --with "import-linter>=2.0" lint-imports --config pyproject.toml

# The custom gates sweep APPLICATION SOURCE only (services/*/src, libs/*/src) —
# the domain where architecture rules #7/#8 bind. Integration tests, the
# migration runner, and operational scripts legitimately use raw asyncpg / env
# and are out of scope (the scripts' own exclusions cover config.py/tests too).
APP_SRC = git ls-files '*.py' | grep -E '^(services|libs)/[^/]+/src/'

check-no-os-environ: ## CI gate — os.environ/os.getenv reads only in config.py
	$(APP_SRC) | xargs uv run python scripts/dev/check-no-os-environ.py

check-no-direct-asyncpg: ## CI gate — raw asyncpg only in libs/db (use tenant_connection)
	$(APP_SRC) | xargs uv run python scripts/dev/check-no-direct-asyncpg.py

reset-db: ## Wipe and recreate the dev Postgres volume (also re-imports the Keycloak realm)
	$(COMPOSE) -f $(COMPOSE_FILE) down -v postgres
	$(COMPOSE) -f $(COMPOSE_FILE) up -d --wait postgres
	# Keycloak stores its realm in the SAME Postgres server (db `keycloak`).
	# Wiping the volume drops Keycloak's schema; without this recreate it keeps
	# a dead connection and every token request fails with `unauthorized_client`
	# (Sprint A1, DEF-A1-16). Force-recreate so it rebuilds schema + re-imports.
	$(COMPOSE) -f $(COMPOSE_FILE) up -d --force-recreate --wait keycloak

migrate-up: ## Apply pending SQL migrations against the dev DB
	uv run python scripts/db/migrate.py up

migrate-down: ## Roll back the most-recently-applied migration
	uv run python scripts/db/migrate.py down

migrate-status: ## Show applied + pending migrations
	uv run python scripts/db/migrate.py status

openapi-dump: ## Refresh docs/api/auth-service-openapi.json from the running app
	uv run python scripts/dev/dump-openapi.py

openapi-check: ## CI gate — fail if the committed OpenAPI snapshot drifts
	uv run python scripts/dev/dump-openapi.py >/dev/null
	@if [ -n "$$(git status --porcelain docs/api/auth-service-openapi.json 2>/dev/null)" ]; then \
	    echo "OpenAPI snapshot is stale. Run: make openapi-dump"; \
	    git diff --no-color docs/api/auth-service-openapi.json | head -50; \
	    exit 1; \
	fi
	@echo "OpenAPI snapshot is up to date."

check-rls: ## CI gate — every user-schema table has RLS+FORCE enabled
	uv run python scripts/ci/check-rls-policies.py

check-audit-insert: ## CI gate — no direct audit.events writes outside libs/audit
	uv run python scripts/ci/check-no-direct-audit-insert.py

check-no-object-storage: ## CI gate — no direct boto3/aioboto3/minio outside libs/storage
	uv run python scripts/ci/check-no-direct-object-storage.py

check-no-crypto: ## CI gate — no direct cryptography.hazmat outside libs/crypto
	uv run python scripts/ci/check-no-direct-crypto.py

seed-prompts: ## Seed medical_prompts (uk/en × 7 specialties)
	psql "postgresql://postgres:postgres@localhost:5432/medical_dictation" \
	    -f infra/postgres/seed/medical_prompts.sql

seed-voice-commands: ## Seed voice_commands from JSON fixtures (sprint 05)
	uv run python scripts/seed/seed_voice_commands.py

seed-abbreviations: ## Seed global abbreviation_dictionary (sprint 05)
	psql "postgresql://postgres:postgres@localhost:5432/medical_dictation" \
	    -f infra/postgres/seed/abbreviations_global.sql

validate-templates: ## CI gate — validate every templates seed JSON (sprint 06)
	PYTHONPATH=libs/template_models/src uv run python scripts/validate-templates.py

seed-templates: ## Seed 16 system templates via upsert_system_template() (sprint 06)
	uv run python scripts/seed/seed_templates.py

wer-eval-per-section: ## Per-section WER eval — global vs section-specific (sprint 06)
	uv run python scripts/eval/run_per_section_wer.py \
	    --fixtures scripts/eval/fixtures/sprint-06-cardiology \
	    --template infra/seeds/templates/cardiology_outpatient_uk.json \
	    --fail-on-no-improvement

wer-eval: ## Run batch WER harness against tests/fixtures/wer (sprint 03)
	uv run python scripts/eval/run_wer.py \
	    --fixtures tests/fixtures/wer \
	    --fail-on-regression

wer-eval-streaming: ## Run streaming WER harness against tests/fixtures/wer (sprint 04)
	uv run python scripts/eval/run_streaming_wer.py \
	    --fixtures tests/fixtures/wer \
	    --fail-on-regression

chaos-dictation: ## Run dictation chaos scenarios (needs dev stack + token)
	RUN_DICTATION_CHAOS=1 uv run --project services/dictation-service pytest tests/chaos/dictation_chaos.py -v

chaos-asr: ## Run batch-ASR worker chaos (SIGKILL reclaim; needs dev stack + asr-service + CPU model + token)
	RUN_ASR_CHAOS=1 uv run --project services/asr-service pytest tests/chaos/asr_chaos.py -v

load-dictation: ## Run dictation load test (needs dev stack + token)
	RUN_DICTATION_LOAD=1 uv run --project services/dictation-service pytest tests/load/ -v

dev-up-asr: ## Start base + dev overlay (no GPU) — useful on laptops
	$(COMPOSE) -f infra/compose/base.yml -f infra/compose/dev.yml up -d --wait

dev-up-gpu: ## Start base + dev + GPU overlay (requires NVIDIA toolkit)
	$(COMPOSE) -f infra/compose/base.yml -f infra/compose/dev.yml -f infra/compose/gpu.yml up -d --wait

nightly-verify: ## Run the audit-chain nightly verifier once and emit Prom textfile
	PROM_TEXTFILE=/tmp/audit_chain.prom uv run python scripts/jobs/nightly_verify.py

test-integration-db: ## All integration tests against the live dev DB (needs migrate-up)
	RUN_DB_INTEGRATION=1 uv run --project libs/db pytest libs/db/tests/integration/ -v
	RUN_DB_INTEGRATION=1 uv run --project libs/audit pytest libs/audit/tests/integration/ -v
	RUN_DB_INTEGRATION=1 RUN_KEYCLOAK_INTEGRATION=1 uv run --project services/auth-service pytest services/auth-service/tests/integration/ -v

ci: lint typecheck test security lint-imports check-no-os-environ check-no-direct-asyncpg check-audit-insert check-no-object-storage check-no-crypto validate-templates ## Mirror CI gates locally

ci-with-db: ci check-rls openapi-check ## Full CI mirror — needs `make dev-up && make migrate-up`

pre-commit-install: ## Install the pre-commit hook into git
	@command -v pre-commit >/dev/null || (echo "Install pre-commit: pip install pre-commit"; exit 1)
	pre-commit install
	pre-commit install --hook-type commit-msg

##@ Misc

help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n"} /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) }' $(MAKEFILE_LIST)
