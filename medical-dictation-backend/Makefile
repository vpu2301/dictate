.PHONY: check-erasure-fanout dsar-cleanup dev-up dev-down dev-restart dev-logs smoke smoke-test lint typecheck typecheck-all type-check test test-cov security security-scan ci doctor reset-db help pre-commit-install lint-imports check-no-os-environ check-no-direct-asyncpg dev-up-asr dev-up-gpu seed-prompts check-no-object-storage check-no-crypto check-no-dev-signing-in-prod-config check-no-demo-envvars-in-prod check-k8s-rendered k8s-render wer-eval check-corpus wer-eval-corpus run-marketing-service

COMPOSE = docker compose
COMPOSE_FILE = docker-compose.yml
# Kept ahead of the dev-stack Prometheus (v2.51.2) on purpose: promtool is
# only used to lint/test rule files, and the newer binary parses everything
# the older server does.
PROMTOOL_IMAGE = prom/prometheus:v2.54.1

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

.PHONY: run-autocomplete-service
run-autocomplete-service: ## Run autocomplete-service on :8007 (needs dev-up + migrate-up + seed)
	OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 \
	uv run --project services/autocomplete-service uvicorn autocomplete_service.main:app --host 0.0.0.0 --port 8007

.PHONY: run-generation-service
run-generation-service: ## Run generation-service on :8009 (needs dev-up + a llama-server/Ollama backend, see ADR-0036)
	OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 \
	uv run --project services/generation-service uvicorn generation_service.main:app --host 0.0.0.0 --port 8009

.PHONY: run-notification-service
run-notification-service: ## Run notification-service on :8004 (needs dev-up + migrate-up + seed)
	OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 \
	uv run --project services/notification-service uvicorn notification_service.main:app --host 0.0.0.0 --port 8004

.PHONY: run-marketing-service
run-marketing-service: ## Run marketing-service on :8012 — public demo booking funnel (needs dev-up + migrate-up)
	OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 \
	uv run --project services/marketing-service uvicorn marketing_service.main:app --host 0.0.0.0 --port 8012

doctor: ## Diagnose local environment issues
	@bash scripts/doctor.sh

##@ Code Quality

lint: ## Run ruff linter across all packages
	uv run ruff check .

lint-fix: ## Run ruff with auto-fix
	uv run ruff check --fix .

typecheck: ## Run mypy --strict over the CI-gated foundation packages (mirrors .github/workflows/ci.yml)
	# corpus-forge is sprint-21 code: new package, no typing-debt exemption.
	uv run --with "mypy>=1.10" mypy --strict services/_template/src/ libs/observability/src/ libs/secret/src/ libs/auth/src/ libs/db/src/ libs/audit/src/ libs/messaging/src/ services/corpus-forge/src/

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
	uv run --project libs/asr_models pytest libs/asr_models/tests/unit/ -v
	uv run --project services/asr-service pytest services/asr-service/tests/unit/ -v
	uv run --project services/asr-worker pytest services/asr-worker/tests/unit/ -v
	uv run --project services/dictation-service pytest services/dictation-service/tests/unit/ -v
	uv run --project services/nlp-service pytest services/nlp-service/tests/unit/ -v
	uv run --project libs/template_models pytest libs/template_models/tests/unit/ -v
	uv run --project libs/report_models pytest libs/report_models/tests/unit/ -v
	uv run --project libs/clinical_access pytest libs/clinical_access/tests/unit/ -v
	uv run --project services/report-service pytest services/report-service/tests/unit/ -v
	uv run --project services/signing-service pytest services/signing-service/tests/unit/ -v
	# kep tests need audit's transitive otel deps — run in the signing-service env.
	uv run --project services/signing-service pytest libs/kep/tests/unit/ -v
	uv run --project libs/notification_events pytest libs/notification_events/tests/unit/ -v
	uv run --project services/notification-service pytest services/notification-service/tests/unit/ -v
	uv run --project services/marketing-service pytest services/marketing-service/tests/unit/ -v
	uv run --project services/core-service pytest services/core-service/tests/unit/ -v
	uv run --project services/corpus-forge pytest services/corpus-forge/tests/unit/ -v

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

openapi-dump: ## Refresh docs/api/*-openapi.json from the running apps
	uv run python scripts/dev/dump-openapi.py

openapi-check: ## CI gate — fail if any committed OpenAPI snapshot drifts
	uv run python scripts/dev/dump-openapi.py >/dev/null
	@if [ -n "$$(git status --porcelain docs/api/*-openapi.json 2>/dev/null)" ]; then \
	    echo "OpenAPI snapshot is stale. Run: make openapi-dump"; \
	    git diff --no-color docs/api/*-openapi.json | head -80; \
	    exit 1; \
	fi
	@echo "OpenAPI snapshots are up to date."

check-rls: ## CI gate — every user-schema table has RLS+FORCE enabled
	uv run python scripts/ci/check-rls-policies.py

check-erasure-fanout: ## CI gate — fan-out map covers every patient-linked table (S11)
	uv run --project services/core-service python scripts/ci/check_erasure_fanout_coverage.py

dsar-cleanup: ## Delete DSAR packages older than DSAR_PACKAGE_TTL_DAYS (cron: infra/compose/cron)
	uv run python scripts/jobs/dsar_package_cleanup.py

check-audit-insert: ## CI gate — no direct audit.events writes outside libs/audit
	uv run python scripts/ci/check-no-direct-audit-insert.py

check-no-object-storage: ## CI gate — no direct boto3/aioboto3/minio outside libs/storage
	uv run python scripts/ci/check-no-direct-object-storage.py

check-no-crypto: ## CI gate — no direct cryptography.hazmat outside libs/crypto
	uv run python scripts/ci/check-no-direct-crypto.py

check-no-dev-signing-in-prod-config: ## CI gate — dev_password signing scaffold never enabled in prod configs
	uv run python scripts/ci/check-no-dev-signing-in-prod-config.py

check-no-demo-envvars-in-prod: ## CI gate (S16) — demo/dev escape hatches never enabled in prod configs
	uv run python scripts/ci/check-no-demo-envvars-in-prod.py

check-k8s-rendered: ## CI gate (S16 deployment) — chart renders; prod render secret-clean; vendored ops files drift-checked
	uv run python scripts/ci/check-k8s-rendered.py

k8s-render: ## Render the Helm chart (staging + prod) to stdout-free temp files for inspection
	helm template mdx infra/k8s/mdx > /tmp/mdx-render-staging.yaml
	helm template mdx infra/k8s/mdx -f infra/k8s/mdx/values-prod.yaml > /tmp/mdx-render-prod.yaml
	@echo "rendered: /tmp/mdx-render-staging.yaml /tmp/mdx-render-prod.yaml"

.PHONY: check-notification-phi-free
check-notification-phi-free: ## CI gate (BLOCKING) — no email template may render PHI
	uv run python scripts/ci/check-notification-phi-free.py

.PHONY: run-notification-digest
run-notification-digest: ## Run the daily notification digest once (cron entrypoint)
	uv run --project services/notification-service python -m notification_service.jobs.digest

seed-prompts: ## Seed medical_prompts (uk/en/de × 7 specialties)
	psql "postgresql://postgres:postgres@localhost:5432/medical_dictation" \
	    -f infra/postgres/seed/medical_prompts.sql

seed-voice-commands: ## Seed voice_commands from JSON fixtures (sprint 05)
	uv run python scripts/seed/seed_voice_commands.py

seed-abbreviations: ## Seed global abbreviation_dictionary (sprint 05)
	psql "postgresql://postgres:postgres@localhost:5432/medical_dictation" \
	    -f infra/postgres/seed/abbreviations_global.sql

validate-templates: ## CI gate — validate every templates seed JSON (sprint 06)
	PYTHONPATH=libs/template_models/src uv run python scripts/validate-templates.py

check-corpus: ## CI gate — WER corpus SHA-256 integrity + PII sweep + eval unit tests (sprint 07)
	uv run python scripts/eval/build_corpus_manifest.py --corpus eval/corpus/v1 --check
	uv run python scripts/eval/check_corpus_pii.py
	uv run pytest scripts/eval/tests/ -q

check-alert-rules: ## CI gate — promtool syntax check + alert unit tests (sprint 14)
	@# Syntax alone is not enough: a valid rule that never fires reads as
	@# "no problem" on a green dashboard. rules/tests/ proves each alert
	@# actually fires on the failure it claims to watch.
	@# --entrypoint sh: docker run does not expand globs (no shell), and
	@# promtool takes explicit paths only.
	docker run --rm -v "$(PWD)/infra/prometheus/rules:/rules:ro" \
	    --entrypoint sh $(PROMTOOL_IMAGE) -c \
	    'promtool check rules /rules/*.yml && promtool test rules /rules/tests/*.yml'

.PHONY: check-metric-names
check-metric-names: ## CI gate — exported metric names must match the declared instruments
	@# rules/tests/ feeds promtool hand-written series, so it proves an alert
	@# fires on the RIGHT SHAPE of data but says nothing about whether the
	@# name it queries is the name the collector actually exports. That gap
	@# is how DictationConversationFleetUnavailable paged on a healthy fleet.
	uv run python scripts/ci/check-metric-names.py

capacity-probe: ## Sprint-14 dual-model capacity probe (residency + per-window latency; CPU on macOS, A10G rig is the gate)
	uv run python scripts/eval/run_capacity_probe.py \
	    --scenarios $(or $(SCENARIOS),1c,2c,2c1d,4d) \
	    $(if $(PROBE_JSON),--json $(PROBE_JSON),)

check-autocomplete-corpus: ## CI gate — validate the autocomplete seed corpus (sprint 10)
	uv run python scripts/validate-autocomplete-corpus.py

.PHONY: corpus-forge
corpus-forge: ## Run the corpus pipeline CLI (sprint 21): make corpus-forge ARGS="gaps"
	uv run --project services/corpus-forge corpus-forge $(ARGS)

.PHONY: fetch-corpus-sources
fetch-corpus-sources: ## Download terminology source snapshots; SHA-256 + date land in the committed lockfile
	uv run python scripts/corpus/fetch_sources.py

.PHONY: check-corpus-releases
check-corpus-releases: ## CI gate — validator v2 over every committed corpus release artifact (sprint 21)
	@set -e; for dir in infra/seeds/corpus/releases/*/; do \
	    v=$$(basename $$dir); \
	    case $$v in v0.*) extra="--skip-quota";; *) extra="";; esac; \
	    echo "validate $$v $$extra"; \
	    uv run --project services/corpus-forge corpus-forge validate --release-dir $$dir $$extra; \
	done

.PHONY: check-corpus-log-hygiene
check-corpus-log-hygiene: ## CI gate — no candidate phrase text / API keys in corpus-pipeline log calls (sprint 21)
	uv run python scripts/ci/check-corpus-log-hygiene.py

.PHONY: check-synonym-corpus
check-synonym-corpus: ## CI gate — validate the medical synonym seed corpus + migration sync (sprint 15)
	uv run python scripts/ci/check_synonym_corpus.py

seed-templates: ## Seed 16 system templates via upsert_system_template() (sprint 06)
	uv run python scripts/seed/seed_templates.py

seed-icd10-fixture: ## Load the committed МКХ-10 fixture (~240 codes) — dev/CI (sprint 13)
	uv run python scripts/load-icd10.py --file infra/seeds/icd10/fixture.csv

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

wer-eval-corpus: ## Run the sprint-07 manifest corpus WER eval (GPU+model on Linux; CPU/tiny auto-fallback on macOS; --dsn optional)
	uv run python scripts/eval/run_wer.py \
	    --corpus eval/corpus \
	    --output eval/reports \
	    $(if $(EVAL_DB_DSN),--dsn $(EVAL_DB_DSN),)

prepare-ecapa: ## Fetch + checksum-verify the pinned ECAPA diarization model (sprint 14, ADR-0034)
	uv run python scripts/models/prepare_ecapa.py

der-eval: ## Run the sprint-14 conversation diarization eval (DER + attribution + latency; CPU on macOS, A10G rig is the gate)
	uv run python scripts/eval/run_der.py \
	    --corpus eval/conversations/v1 \
	    $(if $(DER_JSON),--json $(DER_JSON),)

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

ci: lint typecheck test security lint-imports check-no-os-environ check-no-direct-asyncpg check-audit-insert check-no-object-storage check-no-crypto check-no-dev-signing-in-prod-config check-no-demo-envvars-in-prod check-k8s-rendered check-notification-phi-free validate-templates check-corpus check-autocomplete-corpus check-synonym-corpus check-corpus-releases check-corpus-log-hygiene check-alert-rules check-metric-names ## Mirror CI gates locally

ci-with-db: ci check-rls check-erasure-fanout openapi-check ## Full CI mirror — needs `make dev-up && make migrate-up`

pre-commit-install: ## Install the pre-commit hook into git
	@command -v pre-commit >/dev/null || (echo "Install pre-commit: pip install pre-commit"; exit 1)
	pre-commit install
	pre-commit install --hook-type commit-msg

##@ Misc

help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n"} /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) }' $(MAKEFILE_LIST)
