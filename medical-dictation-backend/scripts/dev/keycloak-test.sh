#!/usr/bin/env bash
# Smoke test: login → introspect → refresh against the dev Keycloak realm.
# Used by CI and by humans to confirm `make dev-up` produced a working IdP.
#
# Exits non-zero on any failure. Prints concise PASS/FAIL lines.
#
# Required tools: curl, jq.
set -euo pipefail

KEYCLOAK_URL="${KEYCLOAK_URL:-http://localhost:8088}"
REALM="${REALM:-medical-dictation}"
CLIENT_ID="${CLIENT_ID:-mdx-dev-cli}"
USERNAME="${USERNAME:-dev-clinician}"
PASSWORD="${PASSWORD:-dev-password}"

TOKEN_URL="${KEYCLOAK_URL}/realms/${REALM}/protocol/openid-connect/token"
INTROSPECT_URL="${KEYCLOAK_URL}/realms/${REALM}/protocol/openid-connect/token/introspect"
JWKS_URL="${KEYCLOAK_URL}/realms/${REALM}/protocol/openid-connect/certs"

fail() { echo "FAIL: $*" >&2; exit 1; }
ok()   { echo "PASS: $*"; }

command -v curl >/dev/null || fail "curl not installed"
command -v jq   >/dev/null || fail "jq not installed"

# ── 1. JWKS reachable ─────────────────────────────────────────────────
jwks=$(curl -sf "${JWKS_URL}") || fail "JWKS endpoint unreachable at ${JWKS_URL}"
kid=$(echo "${jwks}" | jq -r '.keys[0].kid')
[[ -n "${kid}" && "${kid}" != "null" ]] || fail "JWKS returned no keys"
ok "JWKS reachable (kid=${kid})"

# ── 2. Password-grant login ───────────────────────────────────────────
resp=$(curl -sf -X POST "${TOKEN_URL}" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "grant_type=password" \
    -d "client_id=${CLIENT_ID}" \
    -d "username=${USERNAME}" \
    -d "password=${PASSWORD}" \
    -d "scope=openid") || fail "Login failed for ${USERNAME}"

access_token=$(echo "${resp}" | jq -r '.access_token')
refresh_token=$(echo "${resp}" | jq -r '.refresh_token')
[[ -n "${access_token}" && "${access_token}" != "null" ]] || fail "No access_token in response"
[[ -n "${refresh_token}" && "${refresh_token}" != "null" ]] || fail "No refresh_token in response"
ok "Login succeeded for ${USERNAME}"

# ── 3. Decode the access token claims (no signature check; we trust IdP for smoke) ──
payload=$(echo "${access_token}" | awk -F. '{print $2}' | tr '_-' '/+' | base64 -d 2>/dev/null \
          || echo "${access_token}" | awk -F. '{print $2}' | tr '_-' '/+' | { read -r p; n=${#p}; pad=$(((4 - n % 4) % 4)); printf '%s' "${p}$(printf '%.s=' $(seq 1 ${pad}))" | base64 -d; })

for claim in sub tid roles iss aud exp iat sid; do
    val=$(echo "${payload}" | jq -r ".${claim} // empty")
    [[ -n "${val}" ]] || fail "claim '${claim}' missing from access token"
done
ok "Access token has all expected claims (sub, tid, roles, iss, aud, exp, iat, sid)"

iss=$(echo "${payload}" | jq -r '.iss')
[[ "${iss}" == "${KEYCLOAK_URL}/realms/${REALM}" ]] || fail "iss=${iss} unexpected"
ok "iss is correct (${iss})"

aud=$(echo "${payload}" | jq -r '.aud | if type == "array" then join(",") else . end')
[[ "${aud}" == *"mdx-api"* ]] || fail "aud=${aud} does not include mdx-api"
ok "aud includes mdx-api (${aud})"

tid=$(echo "${payload}" | jq -r '.tid')
[[ "${tid}" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || fail "tid=${tid} not a UUID"
ok "tid is a UUID (${tid})"

# ── 4. Introspect (using mdx-backend confidential client) ─────────────
introspect=$(curl -sf -X POST "${INTROSPECT_URL}" \
    -u "mdx-backend:dev-secret-change-in-prod-mdx-backend" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "token=${access_token}") || fail "Introspect call failed"
active=$(echo "${introspect}" | jq -r '.active')
[[ "${active}" == "true" ]] || fail "Introspect reports token inactive"
ok "Token introspect: active"

# ── 5. Refresh-token rotation ─────────────────────────────────────────
refresh_resp=$(curl -sf -X POST "${TOKEN_URL}" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "grant_type=refresh_token" \
    -d "client_id=${CLIENT_ID}" \
    -d "refresh_token=${refresh_token}") || fail "Refresh failed"

new_refresh=$(echo "${refresh_resp}" | jq -r '.refresh_token')
[[ -n "${new_refresh}" && "${new_refresh}" != "null" ]] || fail "Refresh did not return a new refresh_token"
[[ "${new_refresh}" != "${refresh_token}" ]] || fail "Refresh did not rotate (token unchanged)"
ok "Refresh succeeded and refresh_token rotated"

# ── 6. Replay-detection (old refresh now invalid) ─────────────────────
replay_status=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${TOKEN_URL}" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "grant_type=refresh_token" \
    -d "client_id=${CLIENT_ID}" \
    -d "refresh_token=${refresh_token}")
[[ "${replay_status}" == "400" || "${replay_status}" == "401" ]] || fail "Old refresh replay returned ${replay_status}, expected 400/401"
ok "Old refresh_token correctly rejected on replay (HTTP ${replay_status})"

echo ""
echo "All Keycloak smoke checks passed."
