#!/usr/bin/env bash
# smoke-test.sh — verify the full local observability stack is reachable.
# Used in sprint demo: git clone && make dev-up && make smoke-test
set -euo pipefail

TEMPLATE_URL="${TEMPLATE_SERVICE_URL:-http://localhost:8080}"
JAEGER_URL="${JAEGER_URL:-http://localhost:16686}"
PROMETHEUS_URL="${PROMETHEUS_URL:-http://localhost:9090}"
LOKI_URL="${LOKI_URL:-http://localhost:3100}"
GRAFANA_URL="${GRAFANA_URL:-http://localhost:3000}"

PASS=0
FAIL=0

check() {
  local name=$1 url=$2 expected=${3:-}
  local body
  if body=$(curl -sf --max-time 5 "$url"); then
    if [[ -n "$expected" && "$body" != *"$expected"* ]]; then
      printf '\033[31m✗ %s — response did not contain "%s"\033[0m\n' "$name" "$expected"
      FAIL=$((FAIL + 1))
    else
      printf '\033[32m✓ %s\033[0m\n' "$name"
      PASS=$((PASS + 1))
    fi
  else
    printf '\033[31m✗ %s — unreachable at %s\033[0m\n' "$name" "$url"
    FAIL=$((FAIL + 1))
  fi
}

echo "==================================================================="
echo " Smoke Tests — Medical Dictation Backend"
echo "==================================================================="
echo ""

echo "── Infrastructure ─────────────────────────────────────────────────"
check "Jaeger UI"         "$JAEGER_URL/api/services"             ""
check "Prometheus"        "$PROMETHEUS_URL/-/healthy"            "Prometheus Server is Healthy"
check "Grafana"           "$GRAFANA_URL/api/health"              "ok"
check "Loki ready"        "$LOKI_URL/ready"                      "ready"

echo ""
echo "── Template service (start manually: uvicorn template_service.main:app) ──"
check "Healthz" "$TEMPLATE_URL/healthz" '"status":"ok"'
check "Readyz"  "$TEMPLATE_URL/readyz"  '"status":"ready"'

echo ""
echo "─────────────────────────────────────────────────────────────────────"
printf "Results: \033[32m%d passed\033[0m, \033[31m%d failed\033[0m\n" "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]] || exit 1
