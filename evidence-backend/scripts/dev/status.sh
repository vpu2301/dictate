#!/usr/bin/env bash
# What is actually running. `make status`.
#
# The failure this exists to prevent: the evidence services are plain uvicorn
# processes, so when one is simply not started there is no crash, no log and no
# container — the first symptom is a red row in the SPA health panel, and the
# only way to tell an unstarted process from a crashed one was `lsof` by hand.
#
# Listening != ready. Every evidence service asserts its hard dependencies in
# /readyz (rule BE6), so a service can hold a port while honestly reporting 503
# — most often the gateway with no generator behind it. Both columns are shown.
set -uo pipefail

GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; DIM=$'\033[2m'; OFF=$'\033[0m'

listening() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

# Prints one row: name, port, up/down, and the /readyz verdict when it has one.
row() {
  local name="$1" port="$2" path="${3:-}" state ready=""
  if listening "${port}"; then
    state="${GREEN}up${OFF}"
    if [[ -n "${path}" ]]; then
      local code
      code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${port}${path}" 2>/dev/null)"
      case "${code}" in
        200) ready="${GREEN}ready${OFF}" ;;
        000) ready="${YELLOW}no response${OFF}" ;;
        *)   ready="${YELLOW}${code} not ready${OFF}" ;;
      esac
    fi
  else
    state="${RED}down${OFF}"
  fi
  printf '  %-24s %-6s %-18s %s\n' "${name}" ":${port}" "${state}" "${ready}"
}

echo
echo "${DIM}platform (../medical-dictation-backend: make dev-up && migrate-up && seed)${OFF}"
row "postgres"              5432
row "redis"                 6379
row "minio"                 9000
row "keycloak"              8088
row "auth-service"          8000 /readyz

echo
echo "${DIM}evidence infra (make evidence-up)${OFF}"
row "opensearch"            9200 /_cluster/health
row "clamav"                3310
row "searxng"               8888
# The egress proxy is deliberately unpublished (ADR-0005) — nothing outside the
# compose network may use it — so it is checked as a container, not a port.
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^evidence-egress-proxy'; then
  printf '  %-24s %-6s %-18s %s\n' "egress-proxy" "(net)" "${GREEN}up${OFF}" ""
else
  printf '  %-24s %-6s %-18s %s\n' "egress-proxy" "(net)" "${RED}down${OFF}" "${DIM}web fetches fail closed${OFF}"
fi

echo
echo "${DIM}generators (make run-generators)${OFF}"
row "generator.fast"        8081 /health
row "generator.heavy"       8082 /health

echo
echo "${DIM}evidence services${OFF}"
row "evidence-ingest"       8010 /readyz
row "evidence-retrieval"    8011 /readyz
row "evidence-answer"       8013 /readyz
row "evidence-websearch"    8014 /readyz
row "evidence-model-gateway" 8015 /readyz
echo
