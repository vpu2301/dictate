# Evidence AI backend workspace. `make help` is the authoritative target list.
#
# Infra (Postgres/Redis/MinIO/Keycloak/observability) and DB migrations are the
# platform's: run `make dev-up && make migrate-up && make seed` in
# ../medical-dictation-backend. Evidence migrations live in the platform
# migration chain (infra/postgres/migrations/), not here.

.DEFAULT_GOAL := help

.PHONY: help lint lint-fix typecheck test test-integration-db \
        contracts-dump contracts-check check-leaf-imports security ci \
        check-no-direct-model check-no-os-environ evidence-up evidence-down \
        evidence-up-all evidence-build evidence-logs \
        run-model-gateway run-ingest-api run-ingest-worker \
        run-websearch run-answer run-generators run-generators-lite status lint-imports check-qs1 \
        check-prompt-changelog qs1-taint security-web triage-eval measure-latency

help: ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-22s\033[0m %s\n", $$1, $$2}'

evidence-up: ## Start the evidence infra (opensearch, clamav, bucket, egress proxy, searxng)
	@# The egress proxy is the web agent's ONLY network path (ADR-0005): without
	@# it, every fetch fails closed and answers degrade to corpus-only.
	docker compose -f infra/compose/evidence.yaml up -d \
	  opensearch clamav minio-init-evidence egress-proxy searxng

evidence-up-all: ## Start infra AND all five services in compose (one command)
	@# The `services` profile. Ordering is by HEALTH, not by start: the gateway
	@# comes up first (it downloads BGE-M3 on a cold volume — several minutes,
	@# not a hang), then retrieval, then answer. Generators are NOT in here —
	@# there is no Metal in the Docker VM, so they run on the host:
	@#     make run-generators     # separate terminal, before this
	docker compose -f infra/compose/evidence.yaml --profile services up -d --build
	@$(MAKE) --no-print-directory status

evidence-build: ## Build all five service images (context is ~/Desktop/dictate — see the compose header)
	docker compose -f infra/compose/evidence.yaml --profile services build

evidence-logs: ## Tail logs for the compose-run services
	docker compose -f infra/compose/evidence.yaml --profile services logs -f --tail=50

evidence-down: ## Stop the evidence infra (and services, if running)
	docker compose -f infra/compose/evidence.yaml --profile services down

status: ## Show which platform/infra/generator/service ports are actually live
	@bash scripts/dev/status.sh

run-generators: ## Run generator.fast (:8081) + generator.heavy (:8082) — needed by the gateway
	@# ADR-0006's dev pins, served by llama-server on the host. Weights are
	@# ggml-org's own GGUF conversions, fetched once to ~/.cache/huggingface.
	@# NOT the host's Ollama blobs: Ollama writes gemma3 4b/12b with its own
	@# converter and llama-server rejects them (1b happens to load, which is
	@# what makes the platform's :8089 trick look transferable). That :8089
	@# server is not a substitute either — wrong model, 4096-token context.
	@# On a 24 GiB machine use run-generators-lite. See the script header.
	@bash scripts/dev/run_generators.sh

run-generators-lite: ## Same, but gemma-3-4b for BOTH roles (~6 GiB) — for machines that cannot hold the 12b
	@# The ADR-0006 dev pins need ~11.7 GiB of generators plus ~6 GiB of stack.
	@# A 24 GiB laptop does not have it, and overshooting OOM-kills Docker
	@# containers rather than failing cleanly. This pair fits; it is OFF-PIN for
	@# generator.heavy and says so loudly on startup, because a first-token
	@# number measured this way is plumbing-grade and is NOT the NFR gate.
	@EVA_GEN_HEAVY_HF=ggml-org/gemma-3-4b-it-GGUF:Q4_K_M bash scripts/dev/run_generators.sh

run-model-gateway: ## Run evidence-model-gateway on :8015 (loads BGE-M3 on CPU)
	@# MAX_PROMPT_CHARS is lowered from the 120 000 default to fit the dev
	@# generators' launched context (see scripts/dev/run_generators.sh): an
	@# over-long prompt must fail as a clean gateway 413, not as a llama-server
	@# error at synthesis that reads like a prompt bug. The rig keeps 120 000.
	@# For embed/rerank-only work set EVA_GATEWAY_GENERATION_ENABLED=false —
	@# otherwise readyz is 503 without the generators (rule BE6).
	OTEL_SDK_DISABLED=true \
	EVA_GATEWAY_MAX_PROMPT_CHARS=40000 \
	uv run --project services/evidence-model-gateway \
	  uvicorn evidence_model_gateway.main:app --host 0.0.0.0 --port 8015

run-retrieval: ## Run evidence-retrieval on :8011 (needs platform dev-up + evidence-up + run-model-gateway)
	@# CORS_ALLOWED_ORIGINS lets the dictat retrieval playground reach this
	@# service from a dev browser. The service ships CORS-off (/retrieve is an
	@# internal hop) — this target is the opt-in, and only for localhost.
	@#
	@# The 400 ms rerank budget is the RIG target and is left alone on purpose:
	@# eval runs must measure the real constant. On a dev CPU the cross-encoder
	@# blows it on every request, so results come back RRF-fused with
	@# degraded=true and no reranking. That is the degrade path working, not a
	@# fault. To see reranked order while developing, uncomment:
	@#   EVA_RETRIEVAL_RERANK_BUDGET_MS=8000 \
	@#
	@# EVA_RETRIEVAL_LOCAL_TIMEOUT_MS is a different story and IS overridden.
	@# Its 600 ms default is also the rig target, but a dev-CPU BGE-M3 query
	@# embedding takes ~560-600 ms, so the corpus connector lands either side
	@# of the budget at random (measured: 562 ms -> 21 passages, 604 ms -> 0).
	@# Losing that race returns NO evidence, so every answer deflects to
	@# `insufficient_basis` and looks like an empty corpus. The rig keeps 600.
	OTEL_SDK_DISABLED=true \
	EVA_RETRIEVAL_LOCAL_TIMEOUT_MS=8000 \
	CORS_ALLOWED_ORIGINS=http://localhost:5173,http://127.0.0.1:5173 \
	uv run --project services/evidence-retrieval \
	  uvicorn evidence_retrieval.main:app --host 127.0.0.1 --port 8011

run-ingest-api: ## Run evidence-ingest ops API on :8010 (needs platform dev-up + migrate-up)
	OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 \
	MDX_MASTER_KEY_PATH=../medical-dictation-backend/infra/dev/master.key \
	uv run --project services/evidence-ingest uvicorn evidence_ingest.main:app --host 0.0.0.0 --port 8010

run-ingest-worker: ## Run the ingest queue worker
	OTEL_SDK_DISABLED=true \
	MDX_MASTER_KEY_PATH=../medical-dictation-backend/infra/dev/master.key \
	uv run --project services/evidence-ingest python -m evidence_ingest.worker

run-websearch: ## Run evidence-websearch on :8014 (needs platform dev-up + evidence-up)
	@# EVA_ALLOW_DIRECT_EGRESS stays UNSET on purpose: fetches go through the
	@# compose egress proxy. Set EVA_EGRESS_PROXY_URL=http://localhost:3128 only
	@# if you published the proxy port; otherwise run the service in compose.
	OTEL_SDK_DISABLED=true \
	MDX_MASTER_KEY_PATH=../medical-dictation-backend/infra/dev/master.key \
	uv run --project services/evidence-websearch \
	  uvicorn evidence_websearch.main:app --host 0.0.0.0 --port 8014

run-answer: ## Run evidence-answer on :8013 (needs dev-up + migrate-up + gateway + retrieval)
	@# Generation roles must be live: readyz is 503 without them (rule BE6).
	@# For corpus-only work set EVA_ANSWER_WEB_ENABLED=false.
	OTEL_SDK_DISABLED=true \
	MDX_MASTER_KEY_PATH=../medical-dictation-backend/infra/dev/master.key \
	uv run --project services/evidence-answer \
	  uvicorn evidence_answer.main:app --host 0.0.0.0 --port 8013

lint: ## ruff check
	uv run ruff check .

lint-fix: ## ruff check --fix + format
	uv run ruff check . --fix
	uv run ruff format .

typecheck: ## mypy --strict over foundation packages (CI-gated)
	uv run --with "mypy>=1.10" mypy --strict \
	  libs/evidence_models/src/ libs/evidence_safety/src/ libs/models/src/

typecheck-all: ## mypy over service packages too — NON-BLOCKING (tracks feature debt)
	-uv run --project services/evidence-ingest --with "mypy>=1.10" mypy \
	  services/evidence-ingest/src/ services/evidence-model-gateway/src/

test: ## pytest per package (platform pattern), then workspace-level tests
	uv run --project libs/evidence_models pytest libs/evidence_models/tests/ -v
	uv run --project libs/evidence_safety pytest libs/evidence_safety/tests/ -v
	uv run --project libs/models pytest libs/models/tests/ -v
	uv run --project services/evidence-ingest pytest services/evidence-ingest/tests/unit -v
	uv run --project services/evidence-model-gateway pytest services/evidence-model-gateway/tests -v
	uv run --project services/evidence-retrieval pytest services/evidence-retrieval/tests/unit -v
	uv run --project services/evidence-websearch pytest services/evidence-websearch/tests/unit -v
	uv run --project services/evidence-answer pytest services/evidence-answer/tests/unit -v
	@# tests/ holds the cross-cutting suites (qs1 taint, SSRF, egress policy);
	@# they need a service env, so they run under the websearch project.
	uv run --project services/evidence-websearch pytest tests/unit tests/qs1 tests/security -v

test-integration-db: ## DB-gated suite (needs platform dev-up + migrate-up)
	RUN_DB_INTEGRATION=1 uv run pytest tests/integration/ -v

contracts-dump: ## Refresh docs/api/evidence-contracts/*.schema.json from the models
	uv run python -m evidence_models.jsonschema_export docs/api/evidence-contracts

contracts-check: ## CI gate — snapshot drift + additive-only policy
	@rm -rf .contracts-check-tmp
	uv run python -m evidence_models.jsonschema_export .contracts-check-tmp >/dev/null
	@if ! diff -r .contracts-check-tmp docs/api/evidence-contracts \
	        --exclude CHANGELOG.md >/dev/null 2>&1; then \
	    echo "Contract schemas are stale. Run: make contracts-dump"; \
	    diff -r .contracts-check-tmp docs/api/evidence-contracts --exclude CHANGELOG.md | head -40; \
	    rm -rf .contracts-check-tmp; \
	    exit 1; \
	fi
	@rm -rf .contracts-check-tmp
	uv run python scripts/contracts/lint_additive.py

check-leaf-imports: ## CI gate — evidence_models imports only stdlib + pydantic
	uv run python scripts/dev/check_leaf_imports.py

check-no-direct-model: ## CI gate — model access only via libs/models (E12)
	uv run python scripts/dev/check_no_direct_model.py

check-no-os-environ: ## CI gate — env reads only in config.py (E8)
	uv run python scripts/dev/check_no_os_environ.py

lint-imports: ## CI gate — import-linter contracts (E1/E2 + the QS1 import contract)
	uv run --with "import-linter>=2.0" lint-imports --config pyproject.toml

check-qs1: ## CI gate — QS1 layer 1b: snapshot types unnameable in the web path
	uv run python scripts/dev/check_qs1.py

check-prompt-changelog: ## CI gate — a prompt change needs a changelog entry (LM3)
	uv run python scripts/dev/check_prompt_changelog.py

qs1-taint: ## PERMANENT GATE — QS1 layer 3: runtime canary taint harness
	uv run --project services/evidence-websearch pytest tests/qs1 -v

security-web: ## SSRF suite + egress-policy assertions (AC-S04-B-5)
	uv run --project services/evidence-websearch pytest tests/security -v

measure-latency: ## Measure the S04 first-token NFR against a LIVE stack (<=3.5s warm, <=6s cold)
	@# Needs the whole stack up incl. both generators. First token is timed to
	@# the first summary_segment — the first thing a user can read — not to the
	@# header, which lands before retrieval and synthesis and would flatter the
	@# pipeline by seconds.
	uv run python scripts/dev/measure_answer_latency.py

triage-eval: ## Triage set v1 — emergency subset must be 100% (AC-S04-B-3)
	uv run --project services/evidence-answer python eval/triage/run.py

security: ## bandit (blocking) + pip-audit (informational, platform posture)
	uv run --with "bandit[toml]>=1.7.8" bandit -c pyproject.toml -r libs/ services/ scripts/
	# Non-blocking like the platform's make security: findings surface in the
	# log; the current known hit is ecdsa (via the platform's python-jose),
	# which has no fixed release to upgrade to.
	uv run --with "pip-audit>=2.7" pip-audit || true

ci: lint typecheck check-leaf-imports check-no-direct-model check-no-os-environ \
    lint-imports check-qs1 check-prompt-changelog contracts-check openapi-check \
    test security security-web ## Mirror the blocking CI gates locally
	@echo "evidence-backend ci: all gates green"

eval-retrieval: ## Standing retrieval gate: run vs adopted baseline (rule T4)
	uv run --project services/evidence-retrieval python eval/harness/run.py

eval-retrieval-ablation: ## Ablation table (dense/lexical/hybrid/+rerank)
	uv run --project services/evidence-retrieval python eval/harness/run.py --ablation

openapi-dump: ## Refresh docs/api/*-openapi.json from the service apps (E11)
	uv run --project services/evidence-model-gateway python scripts/dev/dump_openapi.py evidence-model-gateway
	uv run --project services/evidence-ingest python scripts/dev/dump_openapi.py evidence-ingest
	uv run --project services/evidence-retrieval python scripts/dev/dump_openapi.py evidence-retrieval
	uv run --project services/evidence-websearch python scripts/dev/dump_openapi.py evidence-websearch
	uv run --project services/evidence-answer python scripts/dev/dump_openapi.py evidence-answer

openapi-check: ## CI gate — fail if any committed OpenAPI snapshot drifts
	@cp -r docs/api /tmp/eva-openapi-before 2>/dev/null || true
	@$(MAKE) openapi-dump >/dev/null
	@if ! diff -q /tmp/eva-openapi-before docs/api >/dev/null 2>&1; then \
	    echo "OpenAPI snapshot is stale. Run: make openapi-dump"; \
	    diff -r /tmp/eva-openapi-before docs/api | head -40; rm -rf /tmp/eva-openapi-before; exit 1; \
	fi
	@rm -rf /tmp/eva-openapi-before
	@echo "OpenAPI snapshots are up to date."
