#!/usr/bin/env bash
# mdx-test.sh — a structured CLI for exercising the medical-dictation backend
# end to end from the terminal.
#
# Every subcommand prints what it runs, the HTTP status / response it got, and
# a coloured PASS/FAIL line. Nothing here mutates production: it targets the
# local dev stack (`make dev-up`) only.
#
#   bash scripts/dev/mdx-test.sh <command> [args]
#   bash scripts/dev/mdx-test.sh help
#
# Most commands honour env overrides so you can point at non-default ports:
#   KEYCLOAK_URL  AUTH_URL  ASR_URL  DICTATION_URL  NLP_URL  REPORT_URL
#   SIGNING_URL   AUTOCOMPLETE_URL  USERNAME  PASSWORD  REALM  CLIENT_ID
set -uo pipefail

# ── Config (override via env) ─────────────────────────────────────────────
KEYCLOAK_URL="${KEYCLOAK_URL:-http://localhost:8088}"
REALM="${REALM:-medical-dictation}"
CLIENT_ID="${CLIENT_ID:-mdx-dev-cli}"
USERNAME="${USERNAME:-dev-clinician}"
PASSWORD="${PASSWORD:-dev-password}"

AUTH_URL="${AUTH_URL:-http://localhost:8000}"          # make run-auth-service
ASR_URL="${ASR_URL:-http://localhost:8001}"            # compose dev overlay
DICTATION_URL="${DICTATION_URL:-http://localhost:8002}"
NLP_URL="${NLP_URL:-http://localhost:8005}"
REPORT_URL="${REPORT_URL:-http://localhost:8006}"
SIGNING_URL="${SIGNING_URL:-http://localhost:8007}"    # run manually
AUTOCOMPLETE_URL="${AUTOCOMPLETE_URL:-http://localhost:8008}"  # run manually

JAEGER_URL="${JAEGER_URL:-http://localhost:16686}"
PROMETHEUS_URL="${PROMETHEUS_URL:-http://localhost:9090}"
GRAFANA_URL="${GRAFANA_URL:-http://localhost:3000}"
LOKI_URL="${LOKI_URL:-http://localhost:3100}"
MINIO_URL="${MINIO_URL:-http://localhost:9000}"

# ── Pretty output ─────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; B=$'\033[34m'; DIM=$'\033[2m'; X=$'\033[0m'
else
  R=""; G=""; Y=""; B=""; DIM=""; X=""
fi
PASS=0; FAIL=0; SKIP=0
pass() { printf "${G}✓ %s${X}\n" "$*"; PASS=$((PASS+1)); }
fail() { printf "${R}✗ %s${X}\n" "$*"; FAIL=$((FAIL+1)); }
skip() { printf "${Y}∅ %s${X}\n" "$*"; SKIP=$((SKIP+1)); }
info() { printf "${B}» %s${X}\n" "$*"; }
run()  { printf "${DIM}\$ %s${X}\n" "$*"; }

need() { command -v "$1" >/dev/null 2>&1 || { echo "${R}missing dependency: $1${X}" >&2; exit 127; }; }

# GET a URL; pass if it returns 2xx (and optionally contains $3).
check_get() {
  local name=$1 url=$2 expect=${3:-}
  local body code
  body=$(curl -s --max-time 6 -w $'\n%{http_code}' "$url" 2>/dev/null)
  code=$(printf '%s' "$body" | tail -n1)
  body=$(printf '%s' "$body" | sed '$d')
  if [[ "$code" =~ ^2 ]]; then
    if [[ -n "$expect" && "$body" != *"$expect"* ]]; then
      fail "$name — 2xx but missing \"$expect\""
    else
      pass "$name ${DIM}($code)${X}"
    fi
  elif [[ "$code" == "000" ]]; then
    skip "$name — unreachable ${DIM}($url)${X}"
  else
    fail "$name — HTTP $code ${DIM}($url)${X}"
  fi
}

# ── Token helper ──────────────────────────────────────────────────────────
get_token() {
  need curl
  local resp at
  resp=$(curl -s --max-time 8 -X POST \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "grant_type=password" -d "client_id=${CLIENT_ID}" \
    -d "username=${USERNAME}" -d "password=${PASSWORD}" -d "scope=openid" \
    "${KEYCLOAK_URL}/realms/${REALM}/protocol/openid-connect/token")
  at=$(printf '%s' "$resp" | python3 -c 'import sys,json
try: print(json.load(sys.stdin)["access_token"])
except Exception: pass' 2>/dev/null)
  if [[ -z "$at" ]]; then
    echo "${R}could not obtain token for ${USERNAME}@${REALM}${X}" >&2
    echo "${DIM}response: ${resp}${X}" >&2
    return 1
  fi
  printf '%s' "$at"
}

# ── Subcommands ───────────────────────────────────────────────────────────
cmd_doctor() {
  info "Checking local prerequisites (delegates to make doctor)"
  run "make doctor"; make doctor
}

cmd_up() {
  info "Bringing the dev stack up (infra + migrations + seed)"
  run "make dev-up && make migrate-up && make seed"
  make dev-up && make migrate-up && make seed
}

cmd_infra() {
  info "Infrastructure health"
  check_get "Jaeger UI"   "$JAEGER_URL/api/services"
  check_get "Prometheus"  "$PROMETHEUS_URL/-/healthy"  "Prometheus Server is Healthy"
  check_get "Grafana"     "$GRAFANA_URL/api/health"    "ok"
  check_get "Loki ready"  "$LOKI_URL/ready"            "ready"
  check_get "MinIO live"  "$MINIO_URL/minio/health/live"
}

cmd_health() {
  info "Service liveness / readiness (skipped = not started)"
  for pair in \
    "auth-service|$AUTH_URL" "asr-service|$ASR_URL" "dictation-service|$DICTATION_URL" \
    "nlp-service|$NLP_URL" "report-service|$REPORT_URL" \
    "signing-service|$SIGNING_URL" "autocomplete-service|$AUTOCOMPLETE_URL"; do
    local name=${pair%%|*} url=${pair##*|}
    check_get "$name /healthz" "$url/healthz" '"status":"ok"'
    check_get "$name /readyz"  "$url/readyz"
  done
}

cmd_token() {
  need python3
  local t; t=$(get_token) || return 1
  info "Access token for ${USERNAME} (${REALM})"
  printf '%s\n' "$t"
  info "Decoded claims"
  printf '%s' "$t" | python3 -c 'import sys,json,base64
p=sys.stdin.read().split(".")[1]; p+="="*(-len(p)%4)
print(json.dumps(json.loads(base64.urlsafe_b64decode(p)), indent=2, ensure_ascii=False))'
}

cmd_keycloak() {
  info "Full Keycloak flow (login → introspect → refresh → replay-reject)"
  run "make keycloak-test"; make keycloak-test
}

cmd_auth() {
  need python3
  info "auth-service login → /auth/me"
  local t code me
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 "$AUTH_URL/healthz" 2>/dev/null)
  [[ "$code" == "000" ]] && { skip "auth-service not reachable ($AUTH_URL) — run: make run-auth-service"; return; }
  info "POST $AUTH_URL/auth/login"
  t=$(curl -s --max-time 8 -X POST "$AUTH_URL/auth/login" \
        -H "Content-Type: application/json" \
        -d "{\"email\":\"${USERNAME}\",\"password\":\"${PASSWORD}\"}" \
      | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("access_token",""))
except Exception: pass')
  [[ -n "$t" ]] && pass "login returned access_token" || { fail "login did not return a token"; return; }
  me=$(curl -s --max-time 6 -H "Authorization: Bearer $t" "$AUTH_URL/auth/me")
  printf '%s\n' "$me" | python3 -m json.tool 2>/dev/null && pass "/auth/me ok" || fail "/auth/me failed"
}

cmd_asr() {
  info "Batch ASR smoke (1s silent WAV → poll job)"
  need ffmpeg
  if [[ -z "${PROMPT_ID:-}" ]]; then
    skip "set PROMPT_ID to a UUID from medical_prompts first, e.g.:"
    echo "${DIM}  PROMPT_ID=\$(psql ... -c \"select id from medical_prompts limit 1\")${X}"
    return
  fi
  ASR_SERVICE_URL="$ASR_URL" KEYCLOAK_URL="$KEYCLOAK_URL" REALM="$REALM" \
    USERNAME="$USERNAME" PASSWORD="$PASSWORD" PROMPT_ID="$PROMPT_ID" \
    bash "$(dirname "$0")/asr-smoke.sh"
}

cmd_nlp() {
  need python3
  info "nlp-service 6-stage pipeline on one Ukrainian segment"
  local t code resp
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 "$NLP_URL/healthz" 2>/dev/null)
  [[ "$code" == "000" ]] && { skip "nlp-service not reachable ($NLP_URL)"; return; }
  t=$(get_token) || return 1
  info "POST $NLP_URL/nlp/process"
  resp=$(curl -s --max-time 10 -X POST "$NLP_URL/nlp/process" \
    -H "Authorization: Bearer $t" -H "Content-Type: application/json" \
    -d '{"text":"тиск сто двадцять на вісімдесят крапка новий рядок","language":"uk","words":[]}')
  printf '%s\n' "$resp" | python3 -m json.tool 2>/dev/null && pass "pipeline returned" || fail "pipeline error: $resp"
}

cmd_autocomplete() {
  need python3
  info "autocomplete-service suggest"
  local t code resp
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 "$AUTOCOMPLETE_URL/healthz" 2>/dev/null)
  [[ "$code" == "000" ]] && { skip "autocomplete-service not reachable ($AUTOCOMPLETE_URL)"; return; }
  t=$(get_token) || return 1
  resp=$(curl -s --max-time 8 -X POST "$AUTOCOMPLETE_URL/autocomplete/suggest" \
    -H "Authorization: Bearer $t" -H "Content-Type: application/json" \
    -d '{"prefix":"паці","language":"uk","limit":5}')
  printf '%s\n' "$resp" | python3 -m json.tool 2>/dev/null && pass "suggest returned" || fail "suggest error: $resp"
}

cmd_signing() {
  info "signing-service public /verify (expects 404 for an unknown token)"
  check_get "verify(unknown)" "$SIGNING_URL/verify/0000000000000000" || true
}

cmd_all() {
  info "── Infrastructure ──"; cmd_infra
  echo; info "── Service health ──"; cmd_health
  echo; info "── Keycloak ──"; get_token >/dev/null 2>&1 && pass "token grant works" || fail "token grant failed"
}

summary() {
  echo
  printf "Results: ${G}%d passed${X}, ${R}%d failed${X}, ${Y}%d skipped${X}\n" "$PASS" "$FAIL" "$SKIP"
  [[ "$FAIL" -eq 0 ]]
}

usage() {
  cat <<EOF
${B}mdx-test${X} — structured CLI for testing the medical-dictation backend

${B}Usage:${X} bash scripts/dev/mdx-test.sh <command>

${B}Stack:${X}
  doctor        Check local prerequisites (make doctor)
  up            dev-up + migrate-up + seed
  infra         Health-check Jaeger/Prometheus/Grafana/Loki/MinIO
  health        Health-check every service /healthz + /readyz
  all           infra + health + keycloak token check

${B}Auth:${X}
  token         Fetch a Keycloak access token and decode its claims
  keycloak      Full login→introspect→refresh→replay flow (make keycloak-test)
  auth          auth-service /auth/login then /auth/me

${B}Per-service flows:${X}
  asr           Batch ASR smoke (needs ffmpeg + PROMPT_ID)
  nlp           Run the NLP pipeline on one segment
  autocomplete  Autocomplete suggest
  signing       Public /verify reachability

${B}Env overrides:${X} KEYCLOAK_URL AUTH_URL ASR_URL NLP_URL REPORT_URL
  SIGNING_URL AUTOCOMPLETE_URL USERNAME PASSWORD REALM CLIENT_ID PROMPT_ID
EOF
}

main() {
  local cmd=${1:-help}; shift || true
  case "$cmd" in
    doctor)       cmd_doctor ;;
    up)           cmd_up ;;
    infra)        cmd_infra; summary ;;
    health)       cmd_health; summary ;;
    all)          cmd_all; summary ;;
    token)        cmd_token ;;
    keycloak)     cmd_keycloak ;;
    auth)         cmd_auth; summary ;;
    asr)          cmd_asr ;;
    nlp)          cmd_nlp; summary ;;
    autocomplete) cmd_autocomplete; summary ;;
    signing)      cmd_signing; summary ;;
    help|-h|--help) usage ;;
    *)            echo "${R}unknown command: $cmd${X}"; echo; usage; exit 2 ;;
  esac
}
main "$@"
