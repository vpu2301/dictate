#!/usr/bin/env bash
# Sprint-10 step-08 load proof: warmup → sustained+burst → cold-storm →
# (optional) Redis-down chaos → Prometheus scrape. Every threshold is
# machine-enforced in k6; a red run exits non-zero here.
#
#   ./scripts/loadtest/run-sprint-10.sh [outdir]
#   RUN_AUTOCOMPLETE_CHAOS=1 ./scripts/loadtest/run-sprint-10.sh   # + chaos stage
#
# k6 runs from the grafana/k6 image (host.docker.internal → local compose).
# The cold-storm flush is PATTERN-SCOPED (autocomplete:trie:*) — the Redis is
# shared with rate-limit and signing keys; never FLUSHALL.
set -euo pipefail

cd "$(dirname "$0")/../.."
OUT="${1:-/tmp/sprint-10-loadtest}"
mkdir -p "$OUT"

K6_IMAGE=grafana/k6:0.57.0
BASE_URL=http://host.docker.internal:8007
AUTH_URL=http://host.docker.internal:8000
PROM=http://localhost:9090

log() { printf '\n== %s ==\n' "$*"; }

login() {
  curl -s -X POST localhost:8000/auth/login -H 'Content-Type: application/json' \
    -d "{\"email\":\"$1\",\"password\":\"dev-password\"}" |
    python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])'
}

prom_query() {
  curl -s "$PROM/api/v1/query" --data-urlencode "query=$1" |
    python3 -c 'import sys,json;r=json.load(sys.stdin)["data"]["result"];print(r[0]["value"][1] if r else "NaN")'
}

k6run() { # scenario name → runs k6, tees summary
  local scenario="$1"
  docker run --rm --add-host=host.docker.internal:host-gateway \
    -v "$PWD/scripts/loadtest:/scripts:ro" "$K6_IMAGE" run \
    -e BASE_URL="$BASE_URL" -e AUTH_URL="$AUTH_URL" -e SCENARIO="$scenario" \
    --summary-trend-stats "avg,p(50),p(95),p(99),max" \
    /scripts/sprint-10-k6.js 2>&1 | tee "$OUT/k6-$scenario.txt"
}

log "environment"
GIT_SHA=$(git rev-parse --short HEAD)
IMG=$(docker inspect medical-dictation-autocomplete-service-1 --format '{{.Image}}' | cut -c8-19)
echo "git=$GIT_SHA image=$IMG date=$(date -u +%FT%TZ)" | tee "$OUT/env.txt"

log "auth (rotating seeded users → distinct trie keys)"
TOKENS="$(login clinician@tenant-a.example),$(login admin@tenant-a.example),$(login admin@tenant-b.example)"

log "warmup (pre-warm per-user/lang trie keys)"
for tok in ${TOKENS//,/ }; do
  for lang in uk en; do
    curl -s -o /dev/null -X POST localhost:8007/autocomplete/suggest \
      -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' \
      -d "{\"prefix\":\"за\",\"language\":\"$lang\",\"limit\":3}"
  done
done

BUILDS_BEFORE=$(prom_query 'sum(mdx_autocomplete_trie_build_seconds_count)')

log "scenario 1+2: sustained 100 RPS / 10m + burst 500 RPS / 1m"
k6run main

log "scenario 3: cold-storm (targeted trie-key flush, then 100 RPS)"
docker exec medical-dictation-redis-1 sh -c \
  "redis-cli --scan --pattern 'autocomplete:trie:*' | xargs -r redis-cli del" >/dev/null
k6run storm

BUILDS_AFTER=$(prom_query 'sum(mdx_autocomplete_trie_build_seconds_count)')
echo "trie builds before=$BUILDS_BEFORE after=$BUILDS_AFTER" | tee "$OUT/builds.txt"

if [ "${RUN_AUTOCOMPLETE_CHAOS:-0}" = "1" ]; then
  log "scenario 4: Redis-down chaos (stop 60s mid-load)"
  ( sleep 30 && docker compose stop redis && sleep 60 && docker compose start redis ) &
  CHAOS_PID=$!
  k6run chaos
  wait "$CHAOS_PID"
  DEGRADED=$(prom_query 'sum(mdx_autocomplete_degraded_total)')
  echo "degraded_total after chaos=$DEGRADED" | tee "$OUT/degraded.txt"
else
  log "chaos stage skipped (set RUN_AUTOCOMPLETE_CHAOS=1)"
fi

log "prometheus p95 (hit path, 15m window)"
prom_query 'histogram_quantile(0.95, sum(rate(mdx_autocomplete_suggest_latency_ms_histogram_bucket{path="hit"}[15m])) by (le))' | tee "$OUT/prom-p95-hit.txt"

log "done — summaries in $OUT"
